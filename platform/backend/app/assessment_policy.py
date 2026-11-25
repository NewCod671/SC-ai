"""Explicit SC assessment basis: onsite notes; attachments are archival only."""
from .finding_style import INSTRUCTION as FINDING_SUMMARY_STYLE
SC_ANSWERS='sc_recorded_answers_v1'
DEFAULT='evidence_required'
SC_GRADING_VERSION='sc-answer-grading-1.17'

# Keep the evidence threshold shared by note coverage and grading across standards.
MATERIAL_FACT_RULES = (
    'Use the selected clause or control (control.id, control.requirement), supplied normative text '
    '(control.standard_knowledge when present), control-specific guidance (control.standard_guidance '
    'when present), and mapped questions as the boundary. When normative text is supplied, checklist '
    'prompts and implementation examples do not add mandatory requirements. '
    'Accept concise descriptions, shorthand, and equivalent safeguards; read the whole note before '
    'deciding a fact is absent. '
    'A fact is material if its answer could change applicability, conformity, or the severity '
    '(NC-Minor vs NC-Major) of a confirmed lapse. '
    'Evaluate the current state recorded at audit time. A confirmed absence, overdue mandatory activity, '
    'or documented exception is a recorded fact; never wait for remediation, deletion, or follow-up '
    'closure to be completed before marking coverage ready or assigning an NC grade. '
    'For a confirmed lapse, infer severity from the recorded scope, sample and operation of the wider process. '
    'A bounded exception in an otherwise operating process can support NC-Minor without an explicit statement '
    'of no effect, a total-population count, or a separate compensating control. A documented absence of a '
    'mandatory control across the relevant scope can support NC-Major when it creates significant doubt '
    'about effective control, without a recorded incident. Do not assume a gap is minor merely because '
    'its impact is not stated. Ask for severity details only when a specific unresolved fact could reasonably '
    'distinguish a bounded lapse from systemic failure or significant doubt about capability. '
    'For example, one missed supplier review with other reviews and access checks operating can be graded '
    'Minor. Active backups do not establish recoverability when a required restore test was missed and the '
    'recovery objective remains unverified. '
    'For A.5.19, "questionnaire pending" already records that the questionnaire is incomplete; '
    'do not ask for its results. Non-production use alone does not establish exclusion. '
    'If that supplier could materially change the scope or severity of a confirmed lapse, ask whether it '
    'is subject to the supplier security review requirement or covered by another evaluation. '
    'When assessing the control, retain the confirmed lapse but use Needs Review if that distinct unresolved '
    'scope fact could change its overall grade. '
        'Ask only for a concrete fact whose absence prevents a decision on an applicable mandatory requirement. '
        'Do not turn examples in the checklist or implementation guidance into required safeguards, '
        'unless the note identifies an applicable obligation or risk that makes the detail material. '
        'For A.7.9, assess actual off-site asset protection; do not require location tracking, a checkout log, '
        'a formal procedure, or protection against shoulder surfing as standalone topics without that basis. '
        'Do not ask whether hypothetical additional off-site asset types exist unless the current or applicable '
        'related notes identify a material asset or off-site use not addressed by the stated safeguards. '
        'For clause 4.4, use the recorded process interactions, monitoring and improvement activity together. '
        'Do not ask for a blanket statement that the whole ISMS conforms or continually improves. '
        'For A.7.2, check how entry is restricted in the recorded areas; do not assume that more secure areas exist '
        'or demand population-wide proof for a bounded visitor-record exception. '
    'Read a statement about badge readers and named access at both server-room and network-room doors as evidence '
    'for both rooms. A separate delivery point does not imply access to a secure area. '
    'For A.5.22, "all in-scope vendors covered" with monitoring described for critical and routine vendors '
    'establishes the monitoring scope. Do not ask for an additional supplier population unless the note identifies an uncovered applicable supplier. '
        'For A.8.5, generic error messages are an example safeguard, not a required coverage topic '
        'unless the note records an applicable policy requirement or a risk tied to authentication responses. '
        'For A.8.28, a time-bounded exception on a named system and explicit controls operating on other active '
        'repositories locate the gap; do not ask whether the same gap hypothetically exists elsewhere. '
    'For conditional controls, check whether a specific applicable triggering event or condition is '
    'recorded before asking what was done. '
)


def answer_based(payload):
    return payload.get('assessment_basis',payload.get('skill',{}).get('assessment_basis'))==SC_ANSWERS


SC_AUDIT_RULES=(
 'Return audit.category (comply|ofi|nc_minor|nc_major|needs_review|not_applicable), short reason, requirement_gap, and system_impact (affects|does_not_affect|unknown). '
 'C/comply: the recorded onsite note establishes the full requirement; set status=met, requirement_gap="", and system_impact=does_not_affect. '
 'OFI: full conformity plus a specific optional improvement; set status=met, requirement_gap="", system_impact=does_not_affect, and give a concise nonempty recommendation. Never use OFI for a required step that is missing. '
 'NC-Minor: a confirmed bounded lapse whose recorded facts support that management system or control-environment capability is maintained; set status=gap or partial, provide the unmet obligation in requirement_gap, and set system_impact=does_not_affect. '
 'NC-Major: an actual systemic breakdown or material effect on management system or control-environment capability; set status=gap or partial, provide the unmet obligation in requirement_gap, and set system_impact=affects only when recorded facts establish that effect. Do not infer severity from control importance. '
 'Needs Review: use audit.category=needs_review and system_impact=unknown when facts are insufficient. If an actual unmet obligation is confirmed but current-state scope or impact is uncertain, retain status=gap or partial and the stated requirement_gap; otherwise use status=needs_review. '
 'Use needs_review only when the note itself lacks facts needed to determine conformity, gap scope, or impact. State that information in the note is insufficient and name the missing fact; an absent attachment is never a reason. '
 'Keep all category/status/field combinations exactly consistent with these values. not_applicable requires status=not_applicable and a written justified exclusion. Do not invent facts or certify compliance.')
REVIEW_PROCEDURE=(
 'SC records these short audit notes after asking the customer and confirming facts onsite. Treat each note as the audit record, not as an unsupported customer self-report. Use only facts written in the note; do not invent observations, tests, scope, dates, or implementation details. '
 'Evaluate each control against the selected standard (and supplied standard_knowledge and standard_guidance when present), the imported Excel checklist questions as baseline prompts, and practical audit considerations for that standard (operating practice vs policy intent, scope, ownership, recency, exceptions). Distinguish mandatory obligations (NC if confirmed unmet; Needs Review if facts are missing) from optional practical enhancements (OFI when mandatory obligations are fulfilled, never NC). '
 'Assess every control by its stable control ID; do not map or assign departments. Question wording is baseline context, not proof. '
 'Files and policies are archival only, are not checked, and are never required for a grade. Return evidence_ids: []. Do not penalize a note for an absent attachment or an unanswered dropdown. '
 'Use needs_review only if the note itself lacks facts needed to verify conformity against the selected standard or material checklist/operational points, gap scope, or impact. Name the specific material missing fact directly, in one short sentence. Never use this for an absent attachment. '
 'Write every result field in English. Combine recorded answers into a direct auditor record in the original checklist style. '
 'Do not claim file verification, invent facts, or certify compliance. '
 +MATERIAL_FACT_RULES+SC_AUDIT_RULES+' '+FINDING_SUMMARY_STYLE)


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
