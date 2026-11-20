"""SC-style report output. Sample report content is never treated as evidence."""
from . import excel

PROFILE = 'sc-audit-note-v1'
HEADERS = ['Control ID', 'Department', 'Answer / Interview', 'Interpreted Answer',
           'Status', 'Finding / Objective Evidence', 'Evidence IDs', 'Action / Follow-up']


def validate(wb, controls):
    if 'Audit Note' not in wb.sheetnames:
        raise ValueError('SC report profile requires Audit Note worksheet')
    ws = wb['Audit Note']
    if [ws.cell(6, i).value for i in range(1, 9)] != HEADERS:
        raise ValueError('Unexpected Audit Note columns')
    if ws.max_row != len(controls) + 6:
        raise ValueError('Audit Note control count differs')
    for row, control in enumerate(controls, 7):
        if ws.cell(row, 1).value != control['id']:
            raise ValueError('Audit Note control order differs')
        if any(ws.cell(row, col).data_type == 'f' or
               any(ws.cell(row, col).coordinate in region for region in ws.merged_cells.ranges)
               for col in range(1, 9)):
            raise ValueError('Audit Note output cells must be unmerged literals')


def patch(parts, controls, answers, findings, statuses, stale, unreviewed, failed, departments):
    path = excel.sheet_path(parts, 'Audit Note')
    root = excel.xml(parts[path])
    for row, c in enumerate(controls, 7):
        cid = c['id']
        finding = findings.get(cid, {}) if not stale and cid not in unreviewed and cid not in failed else {}
        warning = findings.get(cid, {}) if not stale and cid in failed else {}
        values = [cid, (departments or {}).get(cid, c['department']), answers[cid]['text'],
                  finding.get('interpreted_answer', ''), statuses[cid],
                  chr(10).join(x for x in [finding.get('rationale', '') or warning.get('rationale', ''),
                      ('Audit: '+finding['audit']['reason']) if finding.get('audit') else '',
                      ('Requirement gap: '+finding['audit']['requirement_gap']) if finding.get('audit',{}).get('requirement_gap') else ''] if x),
                  ', '.join(finding.get('evidence_ids', [])),
                  finding.get('recommendation', '') or warning.get('recommendation', '')]
        for col, value in enumerate(values, 1):
            text = str(value)
            if len(text) > 32767:
                text = text[:32690] + '\n[Full content in assessment history.]'
            excel.set_cell(root, row, col, text)
    parts[path] = excel.ET.tostring(root, encoding='utf-8', xml_declaration=True)
