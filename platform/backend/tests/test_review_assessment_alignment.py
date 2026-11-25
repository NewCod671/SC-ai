"""Tests for review-note/assessment alignment and multi-standard profile isolation."""
from copy import deepcopy
import json
from pathlib import Path
import httpx
import pytest
from app import (
    agent_mode,
    ai,
    assessment_policy,
    audit_classification,
    codex_provider,
    main,
    standard_profiles,
)
from app.models import Standard
from app.schemas import (
    AddControlQuestionInput,
    CreateAssessment,
    NoteReviewInput,
    ProcessingModeInput,
    ReplaceAnswers,
    RunInput,
)
from .test_codex_provider import context_receipt
from .test_inference_diagnostics import fake_gateway  # noqa: F401
from .test_standard_skills import db  # noqa: F401


def _create_standard(db, *, name, version, controls, basis=assessment_policy.SC_ANSWERS):
    from .fixtures import workbook

    data = workbook([(c['id'], c['requirement'], c.get('department', 'IT')) for c in controls])
    s = Standard(
        name=name,
        version=version,
        controls=controls,
        mapping={'assessment_basis': basis, 'columns': {'control_id': 'Control ID', 'requirement': 'Requirement', 'department': 'Department', 'answer': 'Answer', 'status': 'Status', 'result': 'Result'}},
        template=data,
        changes={},
    )
    db.add(s)
    db.commit()
    return s


def _create_assessment(db, standard_id):
    return main.create_assessment(
        CreateAssessment(
            name='Multi-standard review',
            standard_id=standard_id,
            context={'company': 'Example Corp', 'business_type': 'FinTech', 'departments': ['IT', 'Security']},
        ),
        db,
    )


def test_pci_dss_standard_never_receives_iso27001_knowledge_examples_or_clause_rules(db, monkeypatch, fake_gateway):
    from .test_gateway import gateway

    controls = [
        {'id': '4.1', 'requirement': 'Processes and mechanisms for protecting cardholder data with strong cryptography during transmission are defined.', 'department': 'IT', 'row': 2},
        {'id': '8.3', 'requirement': 'Strong authentication for users and administrators is established and managed.', 'department': 'IT', 'row': 3},
        {'id': '9.1', 'requirement': 'Processes and mechanisms for restricting physical access to cardholder data are defined.', 'department': 'Security', 'row': 4},
    ]
    s = _create_standard(db, name='PCI-DSS', version='4.0.1', controls=controls)
    assert standard_profiles.resolve(s).id == 'generic'

    a = _create_assessment(db, s.id)
    answers = {c['id']: {'status': 'unanswered', 'text': f'PCI implementation note for {c["id"]} with TLS 1.3 and MFA.'} for c in controls}
    a = main.save_answers(a['id'], ReplaceAnswers(expected_version=a['current_version'], answers=answers), db)

    # 1. Codex review-note
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})
    monkeypatch.setattr(codex_provider, 'cli_path', lambda: 'synthetic-codex.exe')
    review_observed = {}

    class ReviewProcess:
        returncode = 0

        def __init__(self, command, stdin, stdout, **kwargs):
            work = Path(kwargs['cwd'])
            review_observed['snapshot'] = json.loads((work / 'context.json').read_text(encoding='utf-8'))
            review_observed['prompt'] = stdin.read().decode('utf-8')
            context_receipt(work, ['get_control'])
            (work / 'result.json').write_text(json.dumps({
                'summary': 'TLS 1.3 recorded.',
                'topics': [{
                    'topic': 'Transmission cryptography',
                    'status': 'covered',
                    'quote': answers['4.1']['text'],
                    'source_control_id': '4.1',
                    'missing_detail': '',
                }],
            }), encoding='utf-8')
            stdout.write((json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}) + '\n').encode())
            stdout.flush()

        def poll(self):
            return 0

    monkeypatch.setattr(codex_provider.subprocess, 'Popen', ReviewProcess)
    review_resp = main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='4.1', text=answers['4.1']['text'], processing_mode='codex', authorize_external=True),
        db,
    )
    assert review_resp['question_coverage'][0]['status'] == 'covered'
    review_ctrl = review_observed['snapshot']['control']['control']
    assert 'standard_knowledge' not in review_ctrl
    assert 'standard_guidance' not in review_ctrl
    assert 'PCI-DSS 4.0.1' in review_observed['prompt']
    for forbidden in ('ISO/IEC 27001', 'ISO/IEC 27002', 'ISMS'):
        assert forbidden not in review_observed['prompt']

    # 2. Codex assessment (batch)
    assess_observed = {}

    class AssessProcess:
        returncode = 0

        def __init__(self, command, stdin, stdout, **kwargs):
            work = Path(kwargs['cwd'])
            assess_observed['snapshot'] = json.loads((work / 'context.json').read_text(encoding='utf-8'))
            assess_observed['prompt'] = stdin.read().decode('utf-8')
            context_receipt(work, ['get_batch_context'])
            findings = [
                {
                    'control_id': c['control_id'],
                    'status': 'met',
                    'rationale': f'Recorded practice for {c["control_id"]}.',
                    'recommendation': '',
                    'evidence_ids': [],
                    'interpreted_answer': 'Implemented',
                    'audit': {
                        'category': 'comply',
                        'reason': 'Full conformity recorded.',
                        'requirement_gap': '',
                        'system_impact': 'does_not_affect',
                    },
                }
                for c in assess_observed['snapshot']['cases']
            ]
            (work / 'result.json').write_text(json.dumps({'findings': findings}), encoding='utf-8')
            stdout.write((json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 10}}) + '\n').encode())
            stdout.flush()

        def poll(self):
            return 0

    monkeypatch.setattr(codex_provider.subprocess, 'Popen', AssessProcess)
    a = main.set_processing_mode(a['id'], ProcessingModeInput(mode='codex', expected_version=a['current_version']), db)
    codex_run = main.run_assessment(a['id'], RunInput(authorize_external=True), db)
    assert codex_run['stale'] is False
    assert assess_observed['snapshot']['standard']['name'] == 'PCI-DSS 4.0.1'
    for case in assess_observed['snapshot']['cases']:
        assert case['control']['grading_examples'] == []
        assert 'standard_knowledge' not in case['control']
        assert 'standard_guidance' not in case['control']
    assert 'PCI-DSS 4.0.1' in assess_observed['prompt']
    for forbidden in ('ISO/IEC 27001', 'ISO/IEC 27002', 'ISMS'):
        assert forbidden not in assess_observed['prompt']

    # 3. Colab assessment & Colab review-note
    a = main.set_processing_mode(a['id'], ProcessingModeInput(mode='colab', expected_version=a['current_version']), db)
    monkeypatch.setenv('SC_ASSESS_BATCH_SIZE', '4')
    monkeypatch.setenv('AI_ASSESS_CONCURRENCY', '1')
    captured_wire = []

    class GatewayClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, json):
            captured_wire.append(json)
            req = gateway.Request(**{k: v for k, v in json.items() if k in {'operation', 'model', 'system', 'payload', 'prompt_profile'}})
            result = gateway.infer(req, 'Bearer test-key')
            return httpx.Response(200, json=result, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx, 'Client', GatewayClient)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(ai.HTTPProvider(url='https://synthetic.example', model='test-model', api_key='test-key')))

    fake_gateway['text'] = json.dumps([
        {
            'control_id': c['id'],
            'status': 'met',
            'interpreted_answer': 'Implemented',
            'rationale': f'Recorded practice for {c["id"]}.',
            'recommendation': '',
            'evidence_ids': [],
            'audit': {'category': 'comply', 'reason': 'Full conformity recorded.', 'requirement_gap': '', 'system_impact': 'does_not_affect'},
        }
        for c in controls
    ])
    colab_run = main.run_assessment(a['id'], RunInput(authorize_external=True), db)
    assert colab_run['stale'] is False
    colab_payload = captured_wire[0]['payload']
    assert colab_payload.get('standard_knowledge') is None
    for c in colab_payload['controls']:
        assert c.get('grading_examples', []) == []
        assert 'standard_guidance' not in c

    fake_gateway['text'] = json.dumps([
        {
            'control_id': '4.1',
            'status': 'needs_review',
            'interpreted_answer': '',
            'rationale': 'TLS 1.3 recorded.',
            'recommendation': json.dumps([
                {'topic': 'Cryptography', 'status': 'covered', 'quote': answers['4.1']['text'], 'source_control_id': '4.1', 'missing_detail': ''}
            ]),
            'evidence_ids': [],
            'audit': {'category': 'needs_review', 'reason': 'Interview coverage only', 'requirement_gap': '', 'system_impact': 'unknown'},
        }
    ])
    colab_review = main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='4.1', text=answers['4.1']['text'], processing_mode='colab', authorize_external=True),
        db,
    )
    assert colab_review['question_coverage'][0]['status'] == 'covered'
    assert captured_wire[1]['payload'].get('standard_knowledge') is None
    assert 'PCI-DSS 4.0.1' in captured_wire[1]['system']


def test_iso27001_review_note_and_codex_assessment_share_exact_inputs_and_clause_guidance(db, monkeypatch):
    controls = [
        {'id': '4.1', 'requirement': '4.1 Understanding the organization and its context', 'department': 'IT', 'row': 2},
        {'id': 'A.5.19', 'requirement': '5.19 Information security in supplier relationships', 'department': 'IT', 'row': 3},
    ]
    s = _create_standard(db, name='ISO/IEC 27001:2022', version='SC-V1', controls=controls)
    a = _create_assessment(db, s.id)
    a = main.add_control_question(
        a['id'],
        AddControlQuestionInput(
            expected_version=a['current_version'],
            control_id='A.5.19',
            text='How do you review active support-ticket suppliers each year?',
            answer_text='Annual review required; Sep review missed for 1 support vendor, other 12 current, SOC alerts active.',
        ),
        db,
    )
    a = main.set_processing_mode(a['id'], ProcessingModeInput(mode='codex', expected_version=a['current_version']), db)
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})

    captured_review = []
    captured_assess = []

    class CaptureCodex:
        name = 'codex:synthetic'

        def __init__(self, bundle=None):
            self.usage_calls = []

        def assess_control(self, case):
            if case.get('note_review_instruction'):
                captured_review.append(deepcopy(case))
                return {
                    'rationale': 'Supplier review process recorded.',
                    'coverage_topics': [
                        {
                            'topic': 'Supplier review scope and safeguards',
                            'status': 'covered',
                            'quote': case['answer']['text'],
                            'source_control_id': 'A.5.19',
                            'missing_detail': '',
                        }
                    ],
                }
            captured_assess.append(deepcopy(case))
            self.usage_calls.append({'prompt_tokens': 10, 'completion_tokens': 5, 'generation_seconds': None})
            return {
                'control_id': case['control_id'],
                'status': 'partial',
                'rationale': 'One supplier review overdue; other 12 current and SOC alerts active.',
                'recommendation': 'Complete overdue vendor review.',
                'evidence_ids': [],
                'interpreted_answer': 'Bounded lapse',
                'audit': {
                    'category': 'nc_minor',
                    'reason': 'Bounded single-supplier lapse with compensating controls.',
                    'requirement_gap': 'Annual review overdue for 1 supplier.',
                    'system_impact': 'does_not_affect',
                },
            }

    monkeypatch.setattr(codex_provider, 'CodexProvider', CaptureCodex)
    note_text = a['answers']['A.5.19']['text']
    main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='A.5.19', text=note_text, processing_mode='codex', authorize_external=True),
        db,
    )
    main.run_assessment(a['id'], RunInput(authorize_external=True), db)

    rev_case = captured_review[0]
    ass_case = next(item for item in captured_assess if item['control_id'] == 'A.5.19')
    assert rev_case['questions'] == ass_case['questions']
    assert any(q.get('source') == 'user' and 'support-ticket suppliers' in q['text'] for q in rev_case['questions'])
    assert rev_case['control']['standard_knowledge'] == ass_case['control']['standard_knowledge']
    assert rev_case['control']['standard_guidance'] == ass_case['control']['standard_guidance']
    assert 'A.5.20' in rev_case['control']['standard_guidance']
    assert rev_case['standard'] == ass_case['standard']
    assert 'departments' not in rev_case['context']
    assert 'departments' not in ass_case['context']


def test_review_note_downgrades_cross_clause_covered_to_partial_for_assessment_consistency(db, monkeypatch):
    controls = [
        {'id': 'A.5.19', 'requirement': '5.19 Information security in supplier relationships', 'department': 'IT', 'row': 2},
        {'id': 'A.5.20', 'requirement': '5.20 Addressing information security within supplier agreements', 'department': 'IT', 'row': 3},
    ]
    s = _create_standard(db, name='ISO/IEC 27001:2022', version='SC-V1', controls=controls)
    a = _create_assessment(db, s.id)
    a = main.save_answers(
        a['id'],
        ReplaceAnswers(
            expected_version=a['current_version'],
            answers={
                'A.5.19': {'status': 'unanswered', 'text': 'Pre-contract supplier security questionnaire collected.'},
                'A.5.20': {'status': 'unanswered', 'text': 'Annual supplier review scheduled and monitored by Procurement for all vendors.'},
            },
        ),
        db,
    )
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})

    class CrossClauseCodex:
        name = 'codex:synthetic'

        def __init__(self, bundle=None):
            self.usage_calls = []

        def assess_control(self, case):
            return {
                'rationale': 'Pre-contract check in A.5.19; ongoing review noted under A.5.20.',
                'coverage_topics': [
                    {
                        'topic': 'Pre-contract supplier checks',
                        'status': 'covered',
                        'quote': 'Pre-contract supplier security questionnaire collected.',
                        'source_control_id': 'A.5.19',
                        'missing_detail': '',
                    },
                    {
                        'topic': 'Ongoing supplier review',
                        'status': 'covered',
                        'quote': 'Annual supplier review scheduled and monitored by Procurement for all vendors.',
                        'source_control_id': 'A.5.20',
                        'missing_detail': '',
                    },
                ],
            }

    monkeypatch.setattr(codex_provider, 'CodexProvider', CrossClauseCodex)
    resp = main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='A.5.19', text=a['answers']['A.5.19']['text'], processing_mode='codex', authorize_external=True),
        db,
    )
    topics = resp['question_coverage']
    assert topics[0]['status'] == 'covered'
    assert topics[0]['source_control_id'] == 'A.5.19'
    assert topics[1]['status'] == 'partial'
    assert topics[1]['source_control_id'] == 'A.5.20'
    assert topics[1]['quote'] == 'Annual supplier review scheduled and monitored by Procurement for all vendors.'
    assert 'A.5.20' in topics[1]['missing_detail']
    assert resp['requires_review'] is True


def test_codex_agent_mode_repairs_classification_field_conflict_once(db, monkeypatch):
    controls = [
        {'id': 'A.8.11', 'requirement': '8.11 Data masking', 'department': 'IT', 'row': 2},
        {'id': 'A.8.13', 'requirement': '8.13 Information backup', 'department': 'IT', 'row': 3},
    ]
    s = _create_standard(db, name='ISO/IEC 27001:2022', version='SC-V1', controls=controls)
    a = _create_assessment(db, s.id)
    a = main.save_answers(
        a['id'],
        ReplaceAnswers(
            expected_version=a['current_version'],
            answers={
                'A.8.11': {'status': 'unanswered', 'text': 'Test DB masked. One legacy support export unmasked for 2 days to 4 users, then removed.'},
                'A.8.13': {'status': 'unanswered', 'text': 'Daily backups ok; billing DB quarterly restore missed; recovery capability unverified.'},
            },
        ),
        db,
    )
    a = main.set_processing_mode(a['id'], ProcessingModeInput(mode='codex', expected_version=a['current_version']), db)
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})
    repair_cases = []

    class RepairingBatchCodex:
        name = 'codex:synthetic'

        def __init__(self, bundle=None):
            self.usage_calls = []

        def assess_controls(self, cases):
            self.usage_calls.append({'prompt_tokens': 100, 'completion_tokens': 25, 'generation_seconds': None, 'control_ids': [c['control_id'] for c in cases]})
            return [
                # Reproduces A.8.11 conflict: status=met alongside nc_minor + requirement_gap
                {
                    'control_id': 'A.8.11',
                    'status': 'met',
                    'rationale': 'Test DB masked; one legacy export unmasked for 2 days then deleted.',
                    'recommendation': 'Enforce automated masking on legacy exports.',
                    'evidence_ids': [],
                    'interpreted_answer': 'Implemented with bounded exception',
                    'audit': {
                        'category': 'nc_minor',
                        'reason': 'Bounded policy lapse for one support export.',
                        'requirement_gap': 'One legacy support export was unmasked.',
                        'system_impact': 'does_not_affect',
                    },
                },
                # Legitimate needs_review with confirmed gap & unknown impact -> must NOT trigger repair
                {
                    'control_id': 'A.8.13',
                    'status': 'partial',
                    'rationale': 'Daily backups ok; billing DB quarterly restore missed.',
                    'recommendation': 'Complete billing DB restore test.',
                    'evidence_ids': [],
                    'interpreted_answer': 'Restore test overdue',
                    'audit': {
                        'category': 'needs_review',
                        'reason': 'Billing DB recoverability unverified.',
                        'requirement_gap': 'Quarterly billing DB restore test missed.',
                        'system_impact': 'unknown',
                    },
                },
            ]

        def assess_control(self, case):
            repair_cases.append(deepcopy(case))
            self.usage_calls.append({'prompt_tokens': 50, 'completion_tokens': 15, 'generation_seconds': None, 'control_ids': [case['control_id']]})
            return {
                'control_id': 'A.8.11',
                'status': 'partial',
                'rationale': 'Test DB masked; one legacy support export unmasked for 2 days then deleted.',
                'recommendation': 'Enforce automated masking on legacy exports.',
                'evidence_ids': [],
                'interpreted_answer': 'Bounded masking lapse',
                'audit': {
                    'category': 'nc_minor',
                    'reason': 'Bounded policy lapse for one support export with remaining masking controls intact.',
                    'requirement_gap': 'One legacy support export was unmasked.',
                    'system_impact': 'does_not_affect',
                },
            }

    monkeypatch.setattr(codex_provider, 'CodexProvider', RepairingBatchCodex)
    monkeypatch.setenv('CODEX_BATCH_SIZE', '5')
    run = main.run_assessment(a['id'], RunInput(authorize_external=True), db)

    assert len(repair_cases) == 1
    assert repair_cases[0]['control_id'] == 'A.8.11'
    assert repair_cases[0]['elevate_reasoning'] is True
    assert 'REPAIR PASS' in repair_cases[0]['repair_instruction']
    plan = run['analysis_plan']
    assert plan['repair_requests'] == 1
    assert plan['requests'] == 2
    assert plan['warnings'] == []
    assert run['usage']['total_tokens'] == 190
    by_id = {row['control_id']: row for row in run['results']}
    assert by_id['A.8.11']['status'] == 'partial'
    assert by_id['A.8.11']['audit']['category'] == 'nc_minor'
    assert by_id['A.8.13']['status'] == 'partial'
    assert by_id['A.8.13']['audit']['category'] == 'needs_review'


def test_audit_normalize_downgrades_conflicting_met_status_to_needs_review():
    conflicting = {
        'control_id': 'A.8.11',
        'status': 'met',
        'rationale': 'Observed',
        'recommendation': 'Fix',
        'evidence_ids': [],
        'audit': {
            'category': 'nc_minor',
            'reason': 'Minor gap',
            'requirement_gap': 'Gap',
            'system_impact': 'does_not_affect',
        },
    }
    assert audit_classification.has_conflict(conflicting, answer_based=True) is not None
    normalized = audit_classification.normalize(deepcopy(conflicting), invalid_reason=audit_classification.CONFLICT_REASON, answer_based=True)
    assert normalized['status'] == 'needs_review'
    assert normalized['audit']['category'] == 'needs_review'
    assert normalized['audit']['reason'] == audit_classification.CONFLICT_REASON

    uncertain_gap = {
        'control_id': 'A.8.13',
        'status': 'partial',
        'rationale': 'Restore missed',
        'recommendation': 'Test restore',
        'evidence_ids': [],
        'audit': {
            'category': 'needs_review',
            'reason': 'Impact unknown',
            'requirement_gap': 'Quarterly test missed',
            'system_impact': 'unknown',
        },
    }
    assert audit_classification.has_conflict(uncertain_gap, answer_based=True) is None
    kept = audit_classification.normalize(deepcopy(uncertain_gap), answer_based=True)
    assert kept['status'] == 'partial'
    assert kept['audit']['category'] == 'needs_review'
