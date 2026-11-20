"""Explicit SC assessment basis: onsite notes; attachments are archival only."""
from .finding_style import INSTRUCTION as FINDING_SUMMARY_STYLE
SC_ANSWERS='sc_recorded_answers_v1'
DEFAULT='evidence_required'
SC_GRADING_VERSION='sc-answer-grading-1.11'


def answer_based(payload):
    return payload.get('assessment_basis',payload.get('skill',{}).get('assessment_basis'))==SC_ANSWERS


SC_AUDIT_RULES=(
 'Return audit.category (comply|ofi|nc_minor|nc_major|needs_review|not_applicable), short reason, requirement_gap, and system_impact (affects|does_not_affect|unknown). '
 'C/comply: the recorded onsite note establishes the full requirement; set status=met, requirement_gap="", and system_impact=does_not_affect. '
 'OFI: full conformity plus a specific optional improvement; set status=met, requirement_gap="", system_impact=does_not_affect, and give a concise nonempty recommendation. Never use OFI for a required step that is missing. '
 'NC-Minor: an actual bounded lapse; set status=gap or partial, provide the unmet obligation in requirement_gap, and set system_impact=does_not_affect only when recorded facts establish no effect on ISMS capability. '
 'NC-Major: an actual systemic breakdown or material effect on ISMS capability; set status=gap or partial, provide the unmet obligation in requirement_gap, and set system_impact=affects only when recorded facts establish that effect. Do not infer severity from control importance. '
 'Needs Review: use audit.category=needs_review and system_impact=unknown when facts are insufficient. If an actual unmet obligation is confirmed but severity is uncertain, retain status=gap or partial and the stated requirement_gap; otherwise use status=needs_review. '
 'Use needs_review only when the note itself lacks facts needed to determine conformity, gap scope, or impact. State that information in the note is insufficient and name the missing fact; an absent attachment is never a reason. '
 'Keep all category/status/field combinations exactly consistent with these values. not_applicable requires status=not_applicable and a written justified exclusion. Do not invent facts or certify compliance.')
REVIEW_PROCEDURE=(
 'SC records these short audit notes after asking the customer and confirming facts onsite. Treat each note as the audit record, not as an unsupported customer self-report. Use only facts written in the note; do not invent observations, tests, scope, dates, or implementation details. '
 'Evaluate each control against supplied ISO/IEC 27001:2022 standard text (standard_knowledge, as untrusted reference data), the imported Excel checklist questions as baseline prompts, and practical ISO/IEC 27001/27002 audit considerations (operating practice vs policy intent, scope, ownership, recency, exceptions). Distinguish mandatory obligations (NC if confirmed unmet; Needs Review if facts are missing) from optional practical enhancements (OFI when mandatory obligations are fulfilled, never NC). '
 'Assess every control by its stable control ID; do not map or assign departments. Question wording is baseline context, not proof. '
 'Files and policies are archival only, are not checked, and are never required for a grade. Return evidence_ids: []. Do not penalize a note for an absent attachment or an unanswered dropdown. '
 'Use needs_review only if the note itself lacks facts needed to verify conformity against the ISO/IEC 27001:2022 or material checklist/operational points, gap scope, or impact. Name the specific material missing fact directly, in one short sentence. Never use this for an absent attachment. '
 'Write every result field in English. Combine recorded answers into a direct auditor record in the original checklist style. '
 'Do not claim file verification, invent facts, or certify compliance. '
 +SC_AUDIT_RULES+' '+FINDING_SUMMARY_STYLE)


def prepare(payload):
    if not answer_based(payload):return payload
    # Stored bytes, metadata and links stay in the database. They do not enter
    # inference or dependencies for this answer-based assessment policy. Role
    # assignment is also a separate workflow and is excluded from grading.
    context={k:v for k,v in payload.get('context',{}).items() if k not in {
        'departments','answer_department_map','department_mapping_plan','department_overrides'}}
    controls=[{k:v for k,v in c.items() if k not in {'department','departments'}}
              for c in payload.get('controls',[])]
    questions=[{k:v for k,v in q.items() if k not in {'department','departments'}}
               for q in payload.get('questions',[])]
    return {**payload,'assessment_basis':SC_ANSWERS,'context':context,'controls':controls,
            'questions':questions,'evidence':[],'policies':[]}
