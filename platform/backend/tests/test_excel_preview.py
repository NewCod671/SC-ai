from io import BytesIO
import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy import select, func
from app import main
from app.reporting import excel
from app.models import Revision, Run
from app.schemas import ReplaceAnswers, RunInput
from .test_standard_skills import db, standard, assessment  # noqa: F401


def workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Visible'
    for i in range(101):
        ws.append([i, '=SUM(1,2)', '<script>alert(1)</script>'])
    wb.create_sheet('Other')['A1'] = 'Other sheet'
    wb.create_sheet('Private').sheet_state = 'hidden'
    wb.create_sheet(excel.META).sheet_state = 'veryHidden'
    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


def test_preview_pages_visible_sheets_and_keeps_formulas_inert():
    data = workbook()
    first = excel.preview(data)
    assert first['sheets'] == ['Visible', 'Other']
    assert first['pages'] == 3
    assert len(first['rows']) == 50
    assert first['rows'][0] == ['0', '=SUM(1,2)', '<script>alert(1)</script>']
    assert first['columns'] == ['A', 'B', 'C']
    last = excel.preview(data, 'Visible', 3)
    assert last['start_row'] == 101
    assert len(last['rows']) == 1
    assert excel.preview(data, 'Other')['rows'] == [['Other sheet']]
    for sheet in ('Private', excel.META, 'Missing'):
        with pytest.raises(ValueError):
            excel.preview(data, sheet)
    for page in (0, 4):
        with pytest.raises(ValueError):
            excel.preview(data, page=page)


def test_preview_preserves_column_widths_and_merges_across_pages():
    wb = Workbook()
    ws = wb.active
    ws.column_dimensions.group('B', 'C')
    ws.column_dimensions['B'].width = 34
    ws['B49'] = 'Shared heading'
    ws.merge_cells('B49:C52')
    stream = BytesIO()
    wb.save(stream)
    first = excel.preview(stream.getvalue())
    second = excel.preview(stream.getvalue(), page=2)
    assert first['column_widths'][1:] == [34, 34]
    assert first['merges'] == [{'row': 48, 'column': 1, 'rowSpan': 2, 'colSpan': 2}]
    assert second['merges'] == [{'row': 0, 'column': 1, 'rowSpan': 2, 'colSpan': 2}]
    assert second['rows'][0][1] == 'Shared heading'


def test_assessment_preview_matches_export_and_never_writes(db):
    s = standard(db)
    a = assessment(db, s.id)
    main.save_answers(a['id'], ReplaceAnswers(expected_version=1, answers={
        c['id']: {'status': 'no', 'text': 'No implementation yet.'} for c in s.controls}), db)
    main.run_assessment(a['id'], RunInput(), db)
    counts = [db.scalar(select(func.count()).select_from(model)) for model in (Revision, Run)]
    result = main.preview_assessment(a['id'], None, 1, db)
    _, exported = main.assessment_workbook(a['id'], db)
    assert result == {**excel.preview(exported), 'version': 2}
    assert excel.META not in result['sheets']
    assert [db.scalar(select(func.count()).select_from(model)) for model in (Revision, Run)] == counts
    answer_column = s.mapping['indices']['answer'] - 1
    status_column = s.mapping['indices']['status'] - 1
    for control in s.controls:
        row = result['rows'][control['row'] - 1]
        assert row[answer_column] == 'No implementation yet.'
        # Offline rules provide an implementation gap but no AI audit grade.
        assert row[status_column] == 'ต้องตรวจเพิ่ม'
    assert main.preview_standard(s.id, None, 1, db)['version'] == s.version


def test_preview_routes_reject_invalid_sheets_and_unknown_assessments(db):
    s = standard(db)
    with pytest.raises(HTTPException) as error:
        main.preview_standard(s.id, excel.META, 1, db)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        main.preview_assessment('missing', None, 1, db)
    assert error.value.status_code == 404
