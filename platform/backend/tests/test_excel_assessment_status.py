from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
import pytest
from lxml import etree as ET
from app import main
from app.reporting import excel
from app.schemas import RunInput, ReplaceAnswers
from .fixtures import workbook, patch_cell
from .test_standard_skills import db, standard, assessment  # noqa: F401


def sample():
    states = list(excel.ASSESSMENT_STATUS_LABELS)
    template = workbook([(f'C-{i}', 'Example requirement', 'IT') for i in range(len(states))])
    controls, mapping = excel.parse_standard(template, {})
    answers = {c['id']: {'status': 'unanswered', 'text': 'Written answer for '+c['id']} for c in controls}
    results = [{'control_id': c['id'], 'status': state, 'rationale': 'Assessment reason',
                'recommendation': 'Next step', 'evidence_ids': []} for c, state in zip(controls, states)]
    manifest = {'assessment_id': 'a', 'standard_id': 's', 'version': 1}
    return template, controls, mapping, answers, results, manifest


def test_excel_status_shows_each_ai_outcome_in_thai_and_roundtrips_original_answers():
    template, controls, mapping, answers, results, manifest = sample()
    data = excel.export(template, mapping, controls, answers, manifest, results=results)
    sheet = excel.read(data)['Checklist']
    assert [sheet.cell(c['row'], mapping['indices']['status']).value for c in controls] == [
        'ผ่าน', 'ไม่ผ่าน', 'ผ่านบางส่วน', 'ต้องตรวจเพิ่ม', 'ไม่เกี่ยวข้อง']
    assert excel.import_answers(data, mapping, controls, manifest, answers) == answers
    assert excel.import_answers(data, mapping, controls, manifest) == answers


def test_missing_or_stale_evaluation_never_exports_self_reported_yes_as_pass():
    template, controls, mapping, answers, results, manifest = sample()
    answers = {cid: {**value, 'status': 'yes'} for cid, value in answers.items()}
    for stale, label in [(False, 'ยังไม่ตรวจ'), (True, 'ต้องประเมินใหม่')]:
        data = excel.export(template, mapping, controls, answers, manifest, results=results if stale else [], stale=stale)
        sheet = excel.read(data)['Checklist']
        assert all(sheet.cell(c['row'], mapping['indices']['status']).value == label for c in controls)
        assert all(sheet.cell(c['row'], mapping['indices']['result']).value in (None, '') for c in controls)
        assert excel.import_answers(data, mapping, controls, manifest, answers) == answers


def test_controls_not_sent_to_ai_show_unchecked_but_reviewed_findings_keep_their_status():
    template, controls, mapping, answers, results, manifest = sample()
    skipped = controls[0]['id']
    data = excel.export(template, mapping, controls, answers, manifest,
                        results=results, unreviewed_ids=[skipped])
    ws = excel.read(data)['Checklist']
    assert ws.cell(controls[0]['row'], mapping['indices']['status']).value == 'ยังไม่ตรวจ'
    assert ws.cell(controls[0]['row'], mapping['indices']['result']).value in (None, '')
    assert ws.cell(controls[3]['row'], mapping['indices']['status']).value == 'ต้องตรวจเพิ่ม'
    assert excel.import_answers(data, mapping, controls, manifest, answers) == answers


def test_answer_edits_are_imported_without_converting_ai_outcomes_to_respondent_status():
    template, controls, mapping, answers, results, manifest = sample()
    data = excel.export(template, mapping, controls, answers, manifest, results=results)
    edited = patch_cell(data, controls[0]['row'], mapping['indices']['answer'], 'Changed implementation')
    imported = excel.import_answers(edited, mapping, controls, manifest, answers)
    assert imported[controls[0]['id']] == {'status': 'unanswered', 'text': 'Changed implementation'}
    # Editing an AI display result does not authorize changing a finding.
    forged = patch_cell(data, controls[1]['row'], mapping['indices']['status'], 'ผ่าน')
    with pytest.raises(ValueError, match='assessment result'):
        excel.import_answers(forged, mapping, controls, manifest, answers)
    # The original editable respondent-status vocabulary remains accepted as a hint.
    self_reported = patch_cell(data, controls[1]['row'], mapping['indices']['status'], 'no')
    assert excel.import_answers(self_reported, mapping, controls, manifest, answers)[controls[1]['id']]['status'] == 'no'


def test_legacy_workbook_with_editable_machine_statuses_is_still_importable():
    template, controls, mapping, answers, _, manifest = sample()
    data = excel.export(template, mapping, controls, answers, manifest)
    for c in controls:
        data = patch_cell(data, c['row'], mapping['indices']['status'], answers[c['id']]['status'])
    with ZipFile(BytesIO(data)) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    meta = ET.fromstring(parts['xl/worksheets/compliance_meta.xml'])
    rows = meta.find(f'{{{excel.NS}}}sheetData')
    for row in list(rows)[1:]:
        rows.remove(row)
    first_row = rows[0]
    for cell in list(first_row):
        if cell.attrib['r'] != 'A1':
            first_row.remove(cell)
    parts['xl/worksheets/compliance_meta.xml'] = ET.tostring(meta)
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    assert excel.import_answers(out.getvalue(), mapping, controls, manifest, answers) == answers


def test_export_route_marks_previous_revision_results_as_stale(db, monkeypatch):
    s = standard(db)
    a = assessment(db, s.id)
    main.run_assessment(a['id'], RunInput(), db)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        cid: {'status': 'unanswered', 'text': 'Updated answer'} for cid in a['answers']}), db)
    captured = []
    def capture(*args, **kwargs):
        captured.append((args, kwargs))
        return b'placeholder'
    monkeypatch.setattr(excel, 'export', capture)
    main.export_assessment(a['id'], db)
    assert captured[0][1]['stale'] is True
    assert captured[0][0][5] == []
