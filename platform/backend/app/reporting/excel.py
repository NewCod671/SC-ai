"""Read with openpyxl; surgically patch OOXML to retain unrelated template parts."""
from io import BytesIO
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED
from copy import deepcopy
import json
import posixpath
from lxml import etree as ET
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pydantic import ValidationError
from ..schemas import Answer

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
META = '_compliance_meta'
MAX_BYTES = 10 * 1024 * 1024
ASSESSMENT_STATUS_LABELS = {'met': 'ผ่าน', 'gap': 'ไม่ผ่าน', 'partial': 'ผ่านบางส่วน',
                            'needs_review': 'ต้องตรวจเพิ่ม', 'not_applicable': 'ไม่เกี่ยวข้อง'}
STATUS_METADATA = 'assessment-status-v1'

def xml(data):
    return ET.fromstring(data, parser=ET.XMLParser(resolve_entities=False, no_network=True))

def inspect_zip(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Workbook exceeds 10 MB')
    try:
        with ZipFile(BytesIO(data)) as z:
            if len(z.namelist()) != len(set(z.namelist())):
                raise ValueError('Workbook contains duplicate ZIP entries')
            if len(z.infolist()) > 2000 or sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024:
                raise ValueError('Workbook expanded size exceeds safe limits')
            if any('vbaProject' in n for n in z.namelist()):
                raise ValueError('Macro-enabled workbooks are unsupported')
    except BadZipFile as exc:
        raise ValueError('Expected a valid .xlsx workbook') from exc

def read(data):
    inspect_zip(data)
    try:
        return load_workbook(BytesIO(data), data_only=False, keep_links=True)
    except Exception as exc:
        raise ValueError('Cannot read workbook') from exc

def preview(data, sheet=None, page=1):
    """Bounded, read-only cells; formulas and links remain inert text."""
    wb = read(data)
    try:
        sheets = [ws.title for ws in wb.worksheets if ws.sheet_state == 'visible' and ws.title != META]
        if not sheets or (sheet is not None and sheet not in sheets):
            raise ValueError('Worksheet is unavailable for preview')
        ws = wb[sheet or sheets[0]]
        if ws.max_row > 10000 or ws.max_column > 200:
            raise ValueError('Preview supports up to 10000 rows and 200 columns per worksheet')
        pages = max(1, (ws.max_row + 49) // 50)
        if page < 1 or page > pages:
            raise ValueError('Preview page is out of range')
        start = (page - 1) * 50 + 1
        end = min(ws.max_row, start + 49)
        rows = [[str(cell.value) if cell.value is not None else '' for cell in row]
                for row in ws.iter_rows(min_row=start, max_row=end, max_col=ws.max_column)]
        merges = []
        for region in ws.merged_cells.ranges:
            top, bottom = max(start, region.min_row), min(end, region.max_row)
            if top <= bottom:
                anchor = ws.cell(region.min_row, region.min_col)
                rows[top - start][region.min_col - 1] = str(anchor.value) if anchor.value is not None else ''
                merges.append({'row': top - start, 'column': region.min_col - 1,
                               'rowSpan': bottom - top + 1, 'colSpan': region.size['columns']})
        widths = [ws.sheet_format.defaultColWidth or 13] * ws.max_column
        for dimension in ws.column_dimensions.values():
            for index in range(dimension.min - 1, min(dimension.max, ws.max_column)):
                widths[index] = dimension.width
        return {'sheets': sheets, 'sheet': ws.title, 'page': page, 'pages': pages,
                'start_row': start, 'total_rows': ws.max_row,
                'merges': merges, 'column_widths': widths,
                'columns': [get_column_letter(i) for i in range(1, ws.max_column + 1)], 'rows': rows}
    finally:
        wb.close()


def parse_standard(data, mapping):
    wb = read(data)
    sheet = mapping.get('sheet', wb.sheetnames[0])
    row = mapping.get('header_row', 1)
    if sheet == META or sheet not in wb.sheetnames or not isinstance(row, int) or row < 1:
        raise ValueError('Invalid worksheet or header row')
    ws = wb[sheet]
    if ws.max_row > 10000 or ws.max_column > 200:
        raise ValueError('Worksheet exceeds 10000 rows or 200 columns')
    fields = {'id': 'Control ID', 'requirement': 'Requirement', 'department': 'Department',
              'answer': 'Answer', 'status': 'Status', 'result': 'Result'} | mapping.get('columns', {})
    if set(fields) - {'id','requirement','department','answer','status','result','question'}:
        raise ValueError('Unknown column mapping')
    headers = [c.value for c in ws[row]]
    columns = {}
    for key, label in fields.items():
        if headers.count(label) != 1:
            raise ValueError(f'Expected exactly one column named {label}')
        columns[key] = headers.index(label) + 1
    if len(set(columns.values())) != len(columns):
        raise ValueError('Each mapped field must use a distinct column')
    controls = []
    for r in range(row + 1, ws.max_row + 1):
        cid = ws.cell(r, columns['id']).value
        requirement = ws.cell(r, columns['requirement']).value
        if cid is None and requirement is None:
            continue
        if not isinstance(cid, str) or not cid.strip() or cid.startswith('=') or len(cid) > 200:
            raise ValueError(f'Row {r}: control ID must be literal nonempty text')
        if not isinstance(requirement, str) or not requirement.strip() or requirement.startswith('=') or len(requirement) > 20000:
            raise ValueError(f'Row {r}: requirement must be literal text')
        if any(c['id'] == cid for c in controls):
            raise ValueError(f'Duplicate control ID: {cid}')
        for key in ('answer', 'status', 'result', 'question'):
            if key in columns and ws.cell(r, columns[key]).data_type == 'f':
                raise ValueError(f'Row {r}: output columns cannot contain formulas')
            if key in columns and any(ws.cell(r, columns[key]).coordinate in region for region in ws.merged_cells.ranges):
                raise ValueError('Mapped output cells cannot be merged')
        department = ws.cell(r, columns['department']).value
        controls.append({'id': cid, 'requirement': requirement, 'department': str(department or 'All'), 'row': r})
    if not controls:
        raise ValueError('No controls found')
    config = {'sheet': sheet, 'header_row': row, 'columns': fields, 'indices': columns}
    profile = mapping.get('report_profile')
    if profile is not None:
        from . import audit_report
        if profile != audit_report.PROFILE:
            raise ValueError('Unknown report profile')
        audit_report.validate(wb, controls)
        config['report_profile'] = profile
    if mapping.get('export_profile') is not None:
        from . import native_sc
        if mapping['export_profile'] != native_sc.PROFILE:
            raise ValueError('Unknown native export profile')
        native_sc.validate_controls(controls)
        config['export_profile'] = native_sc.PROFILE
    return controls, config

def sheet_path(parts, sheet_name):
    workbook = xml(parts['xl/workbook.xml'])
    rels = xml(parts['xl/_rels/workbook.xml.rels'])
    sheet = next(s for s in workbook.find(f'{{{NS}}}sheets') if s.attrib['name'] == sheet_name)
    target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == sheet.attrib[f'{{{DOCREL}}}id'])
    return target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)

def set_cell(root, row_num, col_num, value):
    data = root.find(f'{{{NS}}}sheetData')
    row = next((r for r in data if int(r.attrib['r']) == row_num), None)
    if row is None:
        row = ET.SubElement(data, f'{{{NS}}}row', {'r': str(row_num)})
        data[:] = sorted(data, key=lambda r: int(r.attrib['r']))
    ref = f'{get_column_letter(col_num)}{row_num}'
    cell = next((c for c in row if c.attrib.get('r') == ref), None)
    if cell is None:
        cell = ET.SubElement(row, f'{{{NS}}}c', {'r': ref})
        # OOXML cells must be in column order.
        from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
        row[:] = sorted(row, key=lambda c: column_index_from_string(coordinate_from_string(c.attrib['r'])[0]))
    for child in list(cell):
        cell.remove(child)
    cell.attrib['t'] = 'inlineStr'
    text = ET.SubElement(ET.SubElement(cell, f'{{{NS}}}is'), f'{{{NS}}}t', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
    # inline strings deliberately prevent Excel formula injection.
    text.text = str(value)


def white_cell_fills(parts, root, refs, unlock=False):
    """Give selected output cells an explicit white fill, preserving other styles."""
    if not refs:
        return
    if 'xl/styles.xml' not in parts:
        from openpyxl import Workbook
        defaults = BytesIO()
        Workbook().save(defaults)
        with ZipFile(defaults) as archive:
            parts['xl/styles.xml'] = archive.read('xl/styles.xml')
        rels_path = 'xl/_rels/workbook.xml.rels'
        rels = xml(parts[rels_path])
        rid = 'rComplianceStyles'
        while any(rel.get('Id') == rid for rel in rels):
            rid += 'x'
        ET.SubElement(rels, f'{{{REL}}}Relationship', Id=rid, Type=DOCREL+'/styles', Target='styles.xml')
        parts[rels_path] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
        types_path = '[Content_Types].xml'
        types = xml(parts[types_path])
        ET.SubElement(types, f'{{{CT}}}Override', PartName='/xl/styles.xml',
                      ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
        parts[types_path] = ET.tostring(types, encoding='utf-8', xml_declaration=True)
    styles=xml(parts['xl/styles.xml'])
    xfs=styles.find(f'{{{NS}}}cellXfs')
    fills=styles.find(f'{{{NS}}}fills')
    white_fill=next((i for i,fill in enumerate(fills)
        if (pattern:=fill.find(f'{{{NS}}}patternFill')) is not None
        and pattern.get('patternType')=='solid'
        and (fg:=pattern.find(f'{{{NS}}}fgColor')) is not None
        and fg.get('rgb','').upper() in {'FFFFFFFF','FFFFFF'}),None)
    if white_fill is None:
        white_fill=len(fills)
        fill=ET.SubElement(fills,f'{{{NS}}}fill')
        pattern=ET.SubElement(fill,f'{{{NS}}}patternFill',patternType='solid')
        ET.SubElement(pattern,f'{{{NS}}}fgColor',rgb='FFFFFFFF')
        ET.SubElement(pattern,f'{{{NS}}}bgColor',indexed='64')
        fills.set('count',str(len(fills)))
    replacements={}
    for ref in refs:
        cell=root.find(f'.//{{{NS}}}c[@r="{ref}"]')
        if cell is None:
            continue
        original=int(cell.get('s','0'))
        source=xfs[original]
        protection=source.find(f'{{{NS}}}protection')
        needs_unlock=unlock and (protection is None or protection.get('locked','1')!='0')
        needs_white_fill=int(source.get('fillId','0'))!=white_fill
        if not needs_white_fill and not needs_unlock:
            continue
        if original not in replacements:
            xf=deepcopy(source)
            if needs_white_fill:
                xf.set('fillId',str(white_fill))
                xf.set('applyFill','1')
            if unlock:
                protection=xf.find(f'{{{NS}}}protection')
                if protection is None:
                    protection=ET.SubElement(xf,f'{{{NS}}}protection')
                protection.set('locked','0')
                xf.set('applyProtection','1')
            replacements[original]=len(xfs)
            xfs.append(xf)
        cell.set('s',str(replacements[original]))
    if replacements:
        xfs.set('count',str(len(xfs)))
        parts['xl/styles.xml']=ET.tostring(styles,encoding='utf-8',xml_declaration=True)
    elif white_fill is not None:
        parts['xl/styles.xml']=ET.tostring(styles,encoding='utf-8',xml_declaration=True)


def unprotect_sheet(root):
    """Remove worksheet-level protection from an exported workbook copy."""
    protection=root.find(f'{{{NS}}}sheetProtection')
    if protection is not None:
        root.remove(protection)


def unprotect_all_sheets(parts):
    workbook=xml(parts['xl/workbook.xml'])
    sheets=workbook.find(f'{{{NS}}}sheets')
    for sheet in sheets:
        path=sheet_path(parts,sheet.attrib['name'])
        root=xml(parts[path])
        unprotect_sheet(root)
        parts[path]=ET.tostring(root,encoding='utf-8',xml_declaration=True)

def export(template, mapping, controls, answers, manifest, results=None, questions=None, stale=False, unreviewed_ids=None, failed_ids=None, department_overrides=None, context=None, failure_warnings=None):
    if mapping.get("export_profile") == "sc-original-v1":
        from . import native_sc
        return native_sc.export(controls,answers,manifest,results,questions,stale,unreviewed_ids,failed_ids,department_overrides,context,failure_warnings)
    with ZipFile(BytesIO(template)) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    path = sheet_path(parts, mapping['sheet'])
    root = xml(parts[path])
    findings = {f['control_id']: f for f in results or []}
    displayed_statuses = {}
    unreviewed = set(unreviewed_ids or [])
    failed = set(failed_ids or [])
    from ..questionnaire import text_by_control
    qs = text_by_control(questions or [])
    for c in controls:
        answer = answers[c['id']]
        finding = findings.get(c['id']) if not stale and c['id'] not in unreviewed and c['id'] not in failed else None
        warning = findings.get(c['id']) if not stale and c['id'] in failed else None
        from ..audit_classification import display_status
        displayed_status = (display_status(finding) or ASSESSMENT_STATUS_LABELS[finding['status']]) if finding else ('ต้องประเมินใหม่' if stale else 'ยังไม่ตรวจ')
        displayed_statuses[c['id']] = displayed_status
        values = {'answer': answer['text'], 'status': displayed_status,
                  'result': json.dumps(finding or warning, ensure_ascii=False) if finding or warning else '',
                  'question': qs.get(c['id'], '')}
        if department_overrides and c['id'] in department_overrides:
            values['department'] = department_overrides[c['id']]
        for key, value in values.items():
            if key in mapping['indices']:
                if len(value) > 32767:
                    value = value[:32690] + '\n[Truncated for Excel; full finding available in assessment run history.]'
                set_cell(root, c['row'], mapping['indices'][key], value)
    if 'result' in mapping['indices']:
        result_col=get_column_letter(mapping['indices']['result'])
        white_cell_fills(parts,root,[f'{result_col}{c["row"]}' for c in controls],unlock=True)
    parts[path] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if mapping.get('report_profile') == 'sc-audit-note-v1':
        from . import audit_report
        audit_report.patch(parts, controls, answers, findings, displayed_statuses, stale,
                           unreviewed, failed, department_overrides)
    wb = xml(parts['xl/workbook.xml'])
    sheets = wb.find(f'{{{NS}}}sheets')
    if any(s.attrib['name'] == META for s in sheets):
        raise ValueError('Template uses reserved metadata worksheet name')
    rels = xml(parts['xl/_rels/workbook.xml.rels'])
    rid = 'rComplianceMeta'
    while any(r.attrib['Id'] == rid for r in rels):
        rid += 'x'
    meta_path = 'xl/worksheets/compliance_meta.xml'
    ET.SubElement(sheets, f'{{{NS}}}sheet', {'name': META, 'sheetId': str(max(int(s.attrib['sheetId']) for s in sheets)+1), 'state': 'veryHidden', f'{{{DOCREL}}}id': rid})
    ET.SubElement(rels, f'{{{REL}}}Relationship', {'Id': rid, 'Type': DOCREL+'/worksheet', 'Target': 'worksheets/compliance_meta.xml'})
    types = xml(parts['[Content_Types].xml'])
    ET.SubElement(types, f'{{{CT}}}Override', {'PartName': '/'+meta_path, 'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
    meta = ET.Element(f'{{{NS}}}worksheet')
    ET.SubElement(meta, f'{{{NS}}}sheetData')
    set_cell(meta, 1, 1, json.dumps(manifest))
    # Keep original respondent statuses separate from the visible AI outcome.
    # Row-based metadata avoids exceeding Excel's single-cell text limit on large checklists.
    set_cell(meta, 1, 2, STATUS_METADATA)
    for column, heading in enumerate(('control_id', 'answer_status', 'displayed_status'), 1):
        set_cell(meta, 2, column, heading)
    for row, control in enumerate(controls, 3):
        cid = control['id']
        set_cell(meta, row, 1, cid)
        set_cell(meta, row, 2, answers[cid]['status'])
        set_cell(meta, row, 3, displayed_statuses[cid])
    parts[meta_path] = ET.tostring(meta, encoding='utf-8')
    for name, tree in [('xl/workbook.xml', wb), ('xl/_rels/workbook.xml.rels', rels), ('[Content_Types].xml', types)]:
        parts[name] = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
    unprotect_all_sheets(parts)
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return out.getvalue()

def import_answers(data, mapping, controls, manifest, original_answers=None, department_overrides=None, expected_export=None):
    wb = read(data)
    if mapping.get("export_profile") == "sc-original-v1" and "Clause 4-10" in wb.sheetnames:
        wb.close()
        from . import native_sc
        return native_sc.import_answers(data,controls,manifest,original_answers,expected_export)
    try:
        actual = json.loads(wb[META]['A1'].value)
    except Exception as exc:
        raise ValueError('Missing assessment metadata; start with an exported checklist') from exc
    if actual != manifest:
        raise ValueError('Workbook belongs to another assessment or stale revision')
    incoming, _ = parse_standard(data, {'sheet': mapping['sheet'], 'header_row': mapping['header_row'], 'columns': mapping['columns']})
    original = {c['id']: c for c in controls}
    if {c['id'] for c in incoming} != set(original):
        raise ValueError('Control IDs must exactly match the assessment')
    status_metadata = {}
    meta = wb[META]
    if meta['B1'].value == STATUS_METADATA:
        if meta.max_row != len(controls) + 2:
            raise ValueError('Invalid assessment status metadata')
        for cid, source, displayed in meta.iter_rows(min_row=3, max_row=len(controls)+2, max_col=3, values_only=True):
            if cid not in original or cid in status_metadata or displayed not in {*ASSESSMENT_STATUS_LABELS.values(), 'Comply', 'C', 'OFI', 'NC-Minor', 'NC-Major', 'ยังไม่ตรวจ', 'รอประเมิน', 'ต้องประเมินใหม่'}:
                raise ValueError('Invalid assessment status metadata')
            try:
                source = Answer(status=source).status
            except ValidationError as exc:
                raise ValueError('Invalid respondent status metadata') from exc
            if original_answers is not None and source != original_answers[cid]['status']:
                raise ValueError('Respondent status metadata differs from the saved revision')
            status_metadata[cid] = {'source': source, 'displayed': displayed}
    answers = {}
    ws = wb[mapping['sheet']]
    for c in incoming:
        old = original[c['id']]
        if c['requirement'] != old['requirement'] or c['department'] not in {old['department'], (department_overrides or {}).get(c['id'],old['department'])}:
            raise ValueError('Control requirements and departments cannot be edited in an assessment')
        status = ws.cell(c['row'], mapping['indices']['status']).value or 'unanswered'
        if c['id'] in status_metadata:
            saved_status = status_metadata[c['id']]
            if str(status).strip() == saved_status['displayed']:
                status = saved_status['source']
            elif str(status).strip() in {*ASSESSMENT_STATUS_LABELS.values(), 'Comply', 'C', 'OFI', 'NC-Minor', 'NC-Major', 'ยังไม่ตรวจ', 'รอประเมิน', 'ต้องประเมินใหม่'}:
                raise ValueError('Status is an assessment result; keep it unchanged, edit the answer, and run assessment again')
        value = ws.cell(c['row'], mapping['indices']['answer']).value
        if len(str(value or '')) > 20000:
            raise ValueError('Answer exceeds 20000 characters')
        try:
            answers[c['id']] = Answer(status=str(status).lower().strip(), text=str(value) if value is not None else '').model_dump()
        except ValidationError as exc:
            raise ValueError(f"Invalid answer for {c['id']}") from exc
    return answers
