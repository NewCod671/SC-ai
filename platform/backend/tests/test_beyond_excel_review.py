from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from app import (
    agent_mode,
    ai,
    assessment_policy,
    codex_provider,
    knowledge,
    main,
    note_review_skill,
    prompt_compaction,
)
from app.finding_style import INSTRUCTION as FINDING_SUMMARY_STYLE
from app.schemas import NoteReviewInput, ProcessingModeInput, ReplaceAnswers, RunInput
from app.skill_templates import template_skill
from .test_codex_provider import context_receipt
from .test_inference_diagnostics import fake_gateway  # noqa: F401
from .test_standard_skills import assessment, db, standard  # noqa: F401


def _iso_standard(db):
    s = standard(db)
    s.controls = [
        {
            'id': '4.3',
            'requirement': '4.3 Determining the scope of the information security management system\n\nHas the organization determined the scope of its ISMS?',
            'department': 'IT',
            'row': 2,
        },
        {
            'id': 'A.8.13',
            'requirement': '8.13 Information backup\n\n1. Has organization got approved policy and procedure for managing backup of data?',
            'department': 'IT',
            'row': 3,
        },
    ]
    s.mapping = {**s.mapping, 'assessment_basis': assessment_policy.SC_ANSWERS}
    db.commit()
    return s


def test_note_review_skill_loads_and_covers_beyond_excel():
    skill = note_review_skill.load_note_review_skill()
    assert skill['name'] == 'note-reviewer'
    assert 'standard_knowledge' in skill['instructions']
    assert 'control.standard_guidance' in skill['instructions']
    assert 'never mark a topic `covered` solely from `related_saved_notes`' in skill['instructions']
    assert 'scope/extent' in skill['instructions']
    assert 'Do not judge conformity, severity, C, OFI, NC-Minor, or NC-Major' in skill['instructions']


def test_review_note_colab_mode_attaches_iso_knowledge_and_bypasses_lines_v1(db, monkeypatch, fake_gateway):
    from .test_gateway import gateway

    s = _iso_standard(db)
    a = assessment(db, s.id)
    note_text = 'Scope covers managed cloud hosting at Bangkok office.'
    captured_wire = []
    topics = [
        {
            'topic': 'Scope boundaries and applicability',
            'status': 'partial',
            'quote': 'Scope covers managed cloud hosting at Bangkok office.',
            'source_control_id': '4.3',
            'missing_detail': 'External/internal issues, interested party requirements, and third-party interfaces are not recorded.',
        },
        {
            'topic': 'Documented scope availability',
            'status': 'missing',
            'quote': '',
            'source_control_id': '',
            'missing_detail': 'Whether the ISMS scope is maintained as documented information is not recorded.',
        },
    ]
    fake_gateway['text'] = json.dumps([
        {
            'control_id': '4.3',
            'status': 'needs_review',
            'interpreted_answer': '',
            'rationale': 'Scope location and service recorded, but ISO 4.3 interfaces and documented scope are missing.',
            'recommendation': json.dumps(topics),
            'evidence_ids': [],
            'audit': {
                'category': 'needs_review',
                'reason': 'Interview coverage only',
                'requirement_gap': 'Interfaces, external/internal issue alignment, and documented information status are missing.',
                'system_impact': 'unknown',
            },
        }
    ])

    class GatewayClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, json):
            captured_wire.append(json)
            result = gateway.infer(gateway.Request(**{k: v for k, v in json.items() if k in {'operation', 'model', 'system', 'payload', 'prompt_profile'}}), 'Bearer test-key')
            return httpx.Response(200, json=result, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx, 'Client', GatewayClient)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(ai.HTTPProvider(url='https://synthetic.example', model='test-model', api_key='test-key')))

    response = main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='4.3', text=note_text, processing_mode='colab', authorize_external=True),
        db,
    )

    assert len(captured_wire) == 1
    wire = captured_wire[0]
    assert wire['payload']['note_review_mode'] is True
    assert wire['payload']['standard_knowledge']['entries']['clause:4.3']['text']
    assert 'Statement of Applicability' not in json.dumps(wire['payload']['standard_knowledge'])
    assert FINDING_SUMMARY_STYLE not in wire['system']
    assert 'WIRE OUTPUT CONTRACT' not in fake_gateway['messages'][0]['content']
    assert 'GRADE:' not in fake_gateway['messages'][0]['content']
    assert 'ISO/IEC 27001:2022' in fake_gateway['messages'][0]['content']
    assert fake_gateway['generation_options']['max_new_tokens'] >= 1536
    assert len(response['question_coverage']) == 2
    assert response['question_coverage'][0]['status'] == 'partial'
    assert response['question_coverage'][0]['quote'] == 'Scope covers managed cloud hosting at Bangkok office.'
    assert response['question_coverage'][1]['status'] == 'missing'


def test_review_note_codex_mode_mounts_only_note_reviewer_and_attaches_iso_knowledge(db, monkeypatch):
    s = _iso_standard(db)
    a = assessment(db, s.id)
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})
    monkeypatch.setattr(codex_provider, 'cli_path', lambda: 'synthetic-codex.exe')
    observed = {}

    class Process:
        returncode = 0

        def __init__(self, command, stdin, stdout, **kwargs):
            work = Path(kwargs['cwd'])
            observed['skills'] = sorted(p.name for p in (work / '.agents' / 'skills').iterdir())
            observed['skill_text'] = (work / '.agents' / 'skills' / 'note-reviewer' / 'SKILL.md').read_text(encoding='utf-8')
            observed['snapshot'] = json.loads((work / 'context.json').read_text(encoding='utf-8'))
            observed['prompt'] = stdin.read().decode('utf-8')
            observed['schema'] = json.loads((work / 'schema.json').read_text(encoding='utf-8'))
            context_receipt(work, ['get_control'])
            (work / 'result.json').write_text(json.dumps({
                'summary': 'Bangkok office hosting scope recorded; ISO 4.3 interfaces and documented info missing.',
                'topics': [
                    {
                        'topic': 'Scope boundaries',
                        'status': 'covered',
                        'quote': 'Scope covers managed cloud hosting at Bangkok office.',
                        'source_control_id': '4.3',
                        'missing_detail': '',
                    },
                    {
                        'topic': 'External and internal interfaces and dependencies',
                        'status': 'missing',
                        'quote': '',
                        'source_control_id': '',
                        'missing_detail': 'Interfaces and outsourced dependencies are not recorded.',
                    },
                ],
            }), encoding='utf-8')
            stdout.write((json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 25, 'output_tokens': 15}}) + '\n').encode())
            stdout.flush()

        def poll(self):
            return 0

    monkeypatch.setattr(codex_provider.subprocess, 'Popen', Process)

    response = main.review_clause_note(
        a['id'],
        NoteReviewInput(expected_version=a['current_version'], control_id='4.3', text='Scope covers managed cloud hosting at Bangkok office.', processing_mode='codex', authorize_external=True),
        db,
    )

    assert observed['skills'] == ['note-reviewer']
    assert 'name: note-reviewer' in observed['skill_text']
    assert '$note-reviewer' in observed['prompt']
    assert 'note_review_instruction' not in observed['snapshot']['control']
    assert 'note_review_skill' not in observed['snapshot']['control']
    assert observed['snapshot']['control']['control']['standard_knowledge']['entries']['clause:4.3']['text']
    assert 'NoteCoverageTopic' in json.dumps(observed['schema'])
    assert len(response['question_coverage']) == 2
    assert response['question_coverage'][0]['status'] == 'covered'
    assert response['question_coverage'][1]['status'] == 'missing'


def test_colab_run_with_sc_skill_pins_iso27001_knowledge_and_deduplicates_style(db, monkeypatch):
    monkeypatch.setenv('SC_ASSESS_BATCH_SIZE', '1')
    monkeypatch.setenv('AI_ASSESS_CONCURRENCY', '1')
    s = _iso_standard(db)
    installed = main.install_skill_template(s.id, 'sc-iso27001-audit-review', db)
    a = assessment(db, s.id)
    main.save_answers(
        a['id'],
        ReplaceAnswers(
            expected_version=1,
            answers={
                '4.3': {'status': 'unanswered', 'text': 'Scope covers managed cloud hosting; SC-ISMS-01 v3 signed by MD.'},
                'A.8.13': {'status': 'unanswered', 'text': 'Daily backups encrypted; monthly restore test passed.'},
            },
        ),
        db,
    )
    captured_batches = []
    delegate = ai.HTTPProvider(url='https://synthetic.example', model='test-model', api_key='test-key')

    def fake_post(url, headers, json):
        captured_batches.append(json)
        cid = json['payload']['controls'][0]['id']
        return httpx.Response(200, json={
            'output': [{
                'control_id': cid,
                'status': 'needs_review',
                'rationale': 'Recorded practice captured; verify remaining ISO obligation details.',
                'recommendation': 'Clarify missing ISO scope/restoration details.',
                'evidence_ids': [],
                'audit': {
                    'category': 'needs_review',
                    'reason': 'Additional ISO details required.',
                    'requirement_gap': '',
                    'system_impact': 'unknown',
                },
            }],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10},
        })

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, json):
            return fake_post(url, headers, json)

    monkeypatch.setattr(httpx, 'Client', FakeClient)
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(delegate))

    run = main.run_assessment(a['id'], RunInput(skill_id=installed['id']), db)

    assert len(captured_batches) == 2
    assert 'clause:4.3' in captured_batches[0]['payload']['standard_knowledge']['entries']
    assert 'A.8.13' in captured_batches[1]['payload']['standard_knowledge']['entries']
    assert 'clause:6.1.3' in captured_batches[1]['payload']['standard_knowledge']['entries']
    assert captured_batches[0]['system'].count(FINDING_SUMMARY_STYLE) == 1
    assert 'ISO/IEC 27001:2022' in captured_batches[0]['system']
    shown = main.get_assessment(a['id'], db)['evaluation']['standard_knowledge']
    assert shown['matched_control_count'] == 2
    assert run['skill']['version'] == '3.6'


def test_codex_run_attaches_iso27001_knowledge_and_invalidates_prior_runs_without_it(db, monkeypatch):
    s = _iso_standard(db)
    a = assessment(db, s.id)
    main.save_answers(
        a['id'],
        ReplaceAnswers(
            expected_version=1,
            answers={
                '4.3': {'status': 'unanswered', 'text': 'Scope SC-ISMS-01 v3 approved.'},
                'A.8.13': {'status': 'unanswered', 'text': 'Daily backups run and tested monthly.'},
            },
        ),
        db,
    )
    main.set_processing_mode(a['id'], ProcessingModeInput(mode='codex', expected_version=2), db)
    monkeypatch.setattr(codex_provider, 'health', lambda: {'available': True, 'version': 'synthetic-cli', 'code': 'ready'})
    calls = []

    class FakeCodex:
        name = 'codex:synthetic'

        def __init__(self, bundle=None):
            self.usage_calls = []

        def assess_control(self, case):
            calls.append(deepcopy(case))
            self.usage_calls.append({'prompt_tokens': 10, 'completion_tokens': 5, 'generation_seconds': None})
            return {
                'control_id': case['control_id'],
                'status': 'met',
                'rationale': 'Recorded arrangements address the control.',
                'recommendation': '',
                'evidence_ids': [],
                'interpreted_answer': 'Implemented',
                'audit': {
                    'category': 'comply',
                    'reason': 'Recorded facts establish conformity.',
                    'requirement_gap': '',
                    'system_impact': 'does_not_affect',
                },
            }

    monkeypatch.setattr(codex_provider, 'CodexProvider', FakeCodex)
    run = main.run_assessment(a['id'], RunInput(authorize_external=True), db)

    assert len(calls) == 2
    assert 'clause:4.3' in calls[0]['control']['standard_knowledge']['entries']
    assert 'A.8.13' in calls[1]['control']['standard_knowledge']['entries']
    assert 'clause:6.1.3' in calls[1]['control']['standard_knowledge']['entries']
    assert run['stale'] is False

    from app.models import AgentRun, Assessment
    row = db.get(AgentRun, run['id'])
    snap = deepcopy(row.snapshot)
    snap.pop('standard_knowledge')
    row.snapshot = snap
    db.commit()
    assert agent_mode.run_json(db, db.get(Assessment, a['id']), row)['stale'] is True


def test_codex_skill_bundle_and_compaction_preserve_single_style_block():
    bundle = codex_provider.skill_bundle()
    assert bundle['iso27001-auditor']['version'] == '1.15.0'
    assert bundle['finding-classifier']['version'] == '1.18.0'
    assert 'ISO/IEC 27001:2022' in bundle['iso27001-auditor']['instructions']
    assert 'ISO/IEC 27002' in bundle['finding-classifier']['instructions']
    spec = template_skill('sc-iso27001-audit-review', SimpleNamespace(
        id='std-1', name='SC', version='1', controls=[{'id': '4.3', 'requirement': 'Scope'}],
    ))
    payload = {'controls': [{'id': '4.3', 'requirement': 'Scope'}], 'skill': spec}
    system, wire = prompt_compaction.compact(
        bundle['iso27001-auditor']['instructions'] + '\n' + bundle['finding-classifier']['instructions'] + '\n' + FINDING_SUMMARY_STYLE,
        payload,
    )
    assert system.count(FINDING_SUMMARY_STYLE) == 1
    assert FINDING_SUMMARY_STYLE not in wire['skill']['core_instructions']


def test_review_note_ollama_mode_omits_grading_rules_and_attaches_iso_knowledge(db, monkeypatch):
    from io import BytesIO
    from unittest.mock import patch

    s = _iso_standard(db)
    a = assessment(db, s.id)
    topics = [
        {
            'topic': 'Scope boundaries and applicability',
            'status': 'covered',
            'quote': 'Scope covers managed cloud hosting.',
            'source_control_id': '4.3',
            'missing_detail': '',
        },
        {
            'topic': 'Interfaces and dependencies',
            'status': 'missing',
            'quote': '',
            'source_control_id': '',
            'missing_detail': 'Third-party interfaces are not recorded.',
        },
    ]
    ollama_finding = {
        'control_id': '4.3',
        'status': 'needs_review',
        'interpreted_answer': '',
        'rationale': 'Scope boundary recorded; interfaces missing.',
        'recommendation': json.dumps(topics),
        'evidence_ids': [],
        'audit': {
            'category': 'needs_review',
            'reason': 'Interview coverage only',
            'requirement_gap': 'Third-party interfaces are missing.',
            'system_impact': 'unknown',
        },
    }
    monkeypatch.setattr(ai, 'provider', lambda: ai.ValidatedProvider(ai.OllamaProvider(model='synthetic')))
    with patch('app.ai.build_opener') as opener:
        opener.return_value.open.return_value = BytesIO(json.dumps({
            'done': True,
            'message': {'content': json.dumps({'output': [ollama_finding]})},
        }).encode())
        response = main.review_clause_note(
            a['id'],
            NoteReviewInput(expected_version=a['current_version'], control_id='4.3', text='Scope covers managed cloud hosting.', processing_mode='colab', authorize_external=True),
            db,
        )
        body = json.loads(opener.return_value.open.call_args.args[0].data)

    system = body['messages'][0]['content']
    user = body['messages'][1]['content']
    assert 'note-reviewer' in system
    assert 'ISO/IEC 27001:2022' in system
    assert FINDING_SUMMARY_STYLE not in system
    assert 'clause:4.3' in user
    assert 'note_review_mode' not in user
    assert len(response['question_coverage']) == 2
    assert response['question_coverage'][0]['status'] == 'covered'
    assert response['question_coverage'][1]['status'] == 'missing'
