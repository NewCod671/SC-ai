"""Versioned audit grading contract and conservative consistency checks."""
VERSION = 'audit-grading-1.0'
LABELS = {'comply':'C', 'ofi':'OFI', 'nc_minor':'NC-Minor', 'nc_major':'NC-Major',
          'needs_review':'ต้องตรวจเพิ่ม', 'not_applicable':'ไม่เกี่ยวข้อง'}
RULES = (
 'Return an audit object for every finding: category (comply|ofi|nc_minor|nc_major|needs_review|not_applicable), '
 'reason (one short sentence explaining the grade), requirement_gap (specific unmet obligation, empty if none), '
 'system_impact (affects|does_not_affect|unknown). '
 'Comply: requirement fully met, supported by supplied evidence. OFI: requirement fully met with evidence, '
 'but a specific optional improvement exists; never label an actual unmet requirement as OFI. '
 'NC-Minor: confirmed nonfulfilment, bounded/isolated lapse and supplied facts show it does not affect the '
 'management system capability to achieve intended results. NC-Major: confirmed nonfulfilment affecting that '
 'capability, e.g. demonstrated systemic breakdown or significant doubt about effective process control. '
 'State the exact obligation and facts supporting system impact in reason; do not guess impact or automatically '
 'escalate by control number, risk label, missing file, number of gaps or absent self-report alone. '
 'If scope/impact is unknown use needs_review, retain the underlying gap/partial implementation status. '
 'A number of related minors may indicate a systemic failure, but never aggregate into a major without facts. '
 'Audit categories must agree with status: comply/ofi only met; nc_minor/nc_major only gap or partial; '
 'not_applicable only not_applicable; unclear support/conflicts require needs_review. '
 'Do not invent evidence, facts, deadlines, root causes or a certification decision. '
 'Use the requested language for reason and requirement_gap. These explicit versioned grading criteria '
 'supply the severity criteria requested by the audit skill.'
)


def pending(reason):
    return {'category':'needs_review','reason':reason,'requirement_gap':'','system_impact':'unknown'}


def normalize(finding, invalid_reason=None, *, answer_based=False):
    audit = finding.get('audit')
    if not audit:
        return finding
    audit = {**audit, 'requirement_gap':audit.get('requirement_gap',''), 'system_impact':audit.get('system_impact','unknown')}
    finding['audit'] = audit
    category, status = audit['category'], finding['status']
    # For an answer-based C/OFI there is no requirement gap whose system
    # impact can be graded. Models sometimes leave this irrelevant field as
    # unknown; normalize that one safe combination instead of hiding the grade.
    if (answer_based and category in {'comply','ofi'} and status == 'met'
            and not audit['requirement_gap'].strip() and audit['system_impact'] == 'unknown'):
        audit['system_impact'] = 'does_not_affect'
    invalid = not audit['reason'].strip()
    if category in {'comply','ofi'}:
        invalid |= status != 'met' or bool(audit['requirement_gap'].strip()) or audit['system_impact'] != 'does_not_affect'
        if category == 'ofi':
            invalid |= not finding.get('recommendation','').strip()
    elif category in {'nc_minor','nc_major'}:
        invalid |= status not in {'gap','partial'} or not audit['requirement_gap'].strip()
        invalid |= audit['system_impact'] != ('affects' if category=='nc_major' else 'does_not_affect')
    elif category == 'not_applicable':
        invalid |= status != 'not_applicable'
    if invalid or (status == 'needs_review' and category != 'needs_review'):
        finding['audit'] = pending(invalid_reason or 'ต้องตรวจเหตุผล ข้อกำหนดที่ไม่เป็นไปตามเกณฑ์ หรือผลกระทบต่อระบบก่อนยืนยันระดับ')
    return finding


def complete(findings):
    """No retroactive classification: missing model output remains explicitly pending."""
    return [normalize({**f, 'audit':f.get('audit') or pending(
        'ยังไม่มีผลจัดระดับ audit กรุณาประเมินด้วย gateway ที่รองรับเกณฑ์ audit'
        if f['status'] != 'needs_review' else 'รอข้อมูลหรือการตรวจเพิ่มเติม')}) for f in findings]


def display_status(finding):
    if finding and finding.get('audit'):
        return LABELS[finding['audit']['category']]
    return None
