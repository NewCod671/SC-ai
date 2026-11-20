"""Deterministic internal checklist outcomes, independent of model prose."""
from collections import Counter

RULE_VERSION = 'all-applicable-met-v2'
STATUSES = ('met', 'partial', 'gap', 'needs_review', 'not_applicable')


def summarize(standard, findings):
    expected = {control['id'] for control in standard.controls}
    if not expected or Counter(f['control_id'] for f in findings) != Counter(expected):
        raise ValueError('Summary requires exactly one finding for every imported control')
    if any(f['status'] not in STATUSES for f in findings):
        raise ValueError('Summary contains an unsupported finding status')
    counts = {state: sum(f['status'] == state for f in findings) for state in STATUSES}
    applicable = len(expected) - counts['not_applicable']
    if counts['gap']:
        status, reason = 'failed', 'One or more applicable controls have confirmed gaps.'
    elif counts['needs_review']:
        status, reason = 'needs_review', 'Evidence or answers are insufficient to determine all applicable controls.'
    elif counts['partial']:
        status, reason = 'partial', 'Some applicable controls are only partially implemented.'
    elif not applicable:
        status, reason = 'not_applicable', 'All controls were marked not applicable; this is not a passing result.'
    else:
        status, reason = 'passed', 'Every applicable control is met under this internal assessment rule.'
    audit_findings = [f['audit'] for f in findings if f.get('audit')]
    if any(a['category'] in {'nc_minor','nc_major'} for a in audit_findings):
        status, reason = 'failed', 'Confirmed audit nonconformities require corrective action.'
    elif audit_findings and any(a['category'] == 'needs_review' for a in audit_findings) and status == 'passed':
        status, reason = 'needs_review', 'Audit grading is incomplete; review the pending findings.'
    result = {'status': status, 'reason': reason, 'standard_id': standard.id, 'standard_name': standard.name,
            'standard_version': standard.version, 'total_controls': len(expected), 'applicable_controls': applicable,
            'counts': counts, 'met_percent': round(counts['met'] / applicable * 100, 1) if applicable else None,
            'rule': {'version': RULE_VERSION, 'pass_condition': 'all_applicable_met',
                     'answer_basis': 'ai_free_text_interpretation',
                     'scope': 'imported_checklist_only', 'partial_credit': False, 'certification': False}}

    if any(f.get('audit') for f in findings):
        from .audit_classification import LABELS, VERSION
        result['audit'] = {'version':VERSION, 'counts':{
            category:sum(f.get('audit',{}).get('category') == category for f in findings)
            for category in LABELS},
            'unclassified':sum(not f.get('audit') for f in findings)}
    return result
