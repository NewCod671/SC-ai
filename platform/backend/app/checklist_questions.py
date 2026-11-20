"""Read interview wording from a pinned imported checklist without inference."""
from .questionnaire import normalize_questions
from .reporting import excel, native_sc


def from_standard(standard):
    controls = standard.controls
    mapping = standard.mapping
    texts = {}
    if mapping.get('export_profile') == native_sc.PROFILE:
        refs = native_sc.validate_controls(controls)
        # These requirements contain the exact heading and original column-B text.
        texts = {cid: ref['requirement'].partition('\n\n')[2] for cid, ref in refs.items()}
    elif 'question' in mapping.get('indices', {}):
        workbook = excel.read(standard.template)
        try:
            sheet = workbook[mapping['sheet']]
            for control in controls:
                cell = sheet.cell(control['row'], mapping['indices']['question'])
                if cell.data_type == 'f':
                    raise ValueError('Checklist question cells must contain literal text')
                texts[control['id']] = str(cell.value or '')
        finally:
            workbook.close()
    questions = normalize_questions([{'control_ids': [c['id']],
        'departments': [c.get('department') or 'รอเลือกแผนก'],
        'text': texts.get(c['id']) or c['requirement'], 'mapping_reason': ''} for c in controls], controls)
    # Preserve exact imported wording; do not apply generated-question cleanup.
    for q in questions:
        q.update(source='checklist', text=texts.get(q['control_id']) or next(c['requirement'] for c in controls if c['id']==q['control_id']))
    return questions
