"""Standard-specific profile registry for multi-standard review and grading.

Each compliance standard (for example ISO/IEC 27001:2022, or future profiles
such as PCI-DSS v4.0.1) resolves to an explicit profile controlling normative
knowledge retrieval, calibration examples, clause/requirement guidance, and
prompt terminology. Unrecognized or non-ISO standards fall back to the
standard-agnostic ``generic`` profile so ISO/ISMS rules never leak across
standards whose requirement numbers overlap (e.g., 4.1-10.2).
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from . import knowledge


@dataclass(frozen=True)
class StandardProfile:
    id: str
    version: str
    display_name: str
    guidance_name: str | None
    system_term: str
    knowledge_ref: dict | None
    grading_knowledge_enabled: bool
    clause_rules: dict[str, str] = field(default_factory=dict)


ISO27001_2022_CLAUSE_RULES: dict[str, str] = {
    '4.1': (
        'For clause 4.1, check identification of internal and external issues relevant to the ISMS. '
        'Concrete examples (such as cloud vendor dependency, customer SLAs, a legacy billing system, '
        'or limited on-call staffing) document this when their ISMS relevance is clear in context. '
        'Do not require a separate risk or opportunity register, determination method, explanation of '
        'each issue\'s effect on ISMS outcomes, or issue-to-risk mapping under 4.1, even if a '
        'checklist prompt asks for them or a related 6.1 note mentions risk management. Flag only an '
        'absent issue category (internal or external) or a material ambiguity about ISMS relevance.'
    ),
    '4.2': (
        'For clause 4.2, check three topics: relevant interested parties, their relevant information '
        'security requirements, and which requirements the organization addresses through the ISMS. '
        'Read shorthand labels such as "In ISMS:" and the entries that follow as an explicit '
        'selection. Do not infer that every party listed has a separate requirement or require an '
        'explicit "none" for each party. Flag a specific party\'s missing requirement only when the '
        'record shows an applicable security requirement whose substance is unclear. Do not ask for '
        'a complete exclusion list, selection criteria, communication methods, or staff '
        'understanding under 4.2.'
    ),
    '5.3': (
        'For clause 5.3, check assignment and communication of relevant information security '
        'responsibilities and authorities, including responsibility for ISMS conformity and '
        'reporting ISMS performance to top management. A deputy or backup arrangement is not a '
        'separate mandatory topic unless explicitly required; if a note records "primary owner '
        'assigned, no deputy yet", the appointment status is documented—do not request a deputy '
        'name, backup procedure, or appointment date.'
    ),
    '9.1': (
        'For clause 9.1, KPI tracking and individual control checks alone do not establish that '
        'their results are used to evaluate ISMS effectiveness. Accept a concise description of '
        'that evaluation with no required wording.'
    ),
    'A.5.3': (
        'For Annex A.5.3, check separation of conflicting duties using the roles, workflows, and '
        'exceptions actually in scope. Concrete examples plus a described role-review or '
        'conflict-check process can document the topic; do not ask for unspecified duties "beyond '
        'the examples" unless the note or standard identifies a material case not covered. When an '
        'exception or combination of duties is recorded, check which duties overlap, what '
        'independent oversight or compensating safeguard is recorded (or explicitly absent), and '
        'the scope and impact of the overlap; do not require duties to be separated before '
        'evaluating conformity or marking coverage complete.'
    ),
    'A.5.19': (
        'For Annex A.5.19, pre-contract checks together with recurring or change-triggered review '
        'and operating safeguards describe how supplier risks are managed after selection. Whether '
        'a supplier signed contractual security terms is a separate topic under A.5.20 and does '
        'not create a missing topic or gap under A.5.19. When a recurring supplier review is '
        'recorded as missed or overdue, check whether the note records the affected scope (how '
        'many in-scope suppliers are affected versus operating on time) and whether active '
        'safeguards or monitoring remained in place (without waiting for follow-up closure).'
    ),
    'A.5.22': (
        'For Annex A.5.22, critical-supplier reviews alone do not establish how other applicable '
        'supplier services are covered. Accept documented risk-based monitoring or a clear '
        'statement that the reviewed suppliers are the applicable scope. If the note establishes '
        'neither, ask how other applicable suppliers are monitored instead of requiring the same '
        'frequency for every supplier.'
    ),
    'A.5.35': (
        'For Annex A.5.35, use independence, review scope, and applicable timing as the required '
        'topics. A recorded independent review at planned intervals covers timing when no '
        'significant change relevant to that review is identified; do not create a missing topic '
        'about hypothetical changes or require a statement that none occurred. If a specific '
        'significant change is recorded in the same scope, check whether an independent review '
        'addressed it. Who commissioned the review, detailed reporting recipients, and whether it '
        'included a separate improvement-opportunity analysis are implementation guidance, not '
        'missing topics or grade blockers.'
    ),
    'A.7.4': (
        'For Annex A.7.4, a known blind spot matters if it is a plausible unauthorized access '
        'route and ongoing detection there is not established. Ask about that route and alternate '
        'monitoring, not camera coverage of every room or location. Do not assume the blind spot '
        'is harmless or a confirmed failure.'
    ),
}


# Future standards (for example "pci-dss-4.0.1") can register their own
# StandardProfile entry here with a dedicated knowledge_ref, clause_rules, and
# optional calibration cases without affecting ISO/IEC 27001 or other standards.
PROFILES: dict[str, StandardProfile] = {
    'iso27001-2022': StandardProfile(
        id='iso27001-2022',
        version='1.0.0',
        display_name='ISO/IEC 27001:2022',
        guidance_name='ISO/IEC 27002',
        system_term='ISMS',
        knowledge_ref=knowledge.ISO27001_2022_REF,
        grading_knowledge_enabled=True,
        clause_rules=ISO27001_2022_CLAUSE_RULES,
    ),
    'generic': StandardProfile(
        id='generic',
        version='1.0.0',
        display_name='',
        guidance_name=None,
        system_term='compliance control environment',
        knowledge_ref=None,
        grading_knowledge_enabled=False,
        clause_rules={},
    ),
}

_NON_ISO_STANDARD_RE = re.compile(
    r'\b(?:pci|dss|soc\s*2|soc2|nist|hipaa|gdpr|pdpa|cis|cobit|27701|22301|9001|42001)\b',
    re.I,
)
_ISO_ANNEX_CONTROL_RE = re.compile(r'^A\.[5-8]\.\d+$')


def resolve(standard) -> StandardProfile:
    if standard is None:
        return PROFILES['generic']
    mapping = getattr(standard, 'mapping', None) or {}
    explicit = mapping.get('standard_profile') if isinstance(mapping, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return PROFILES.get(explicit.strip(), PROFILES['generic'])
    name_ver = f"{getattr(standard, 'name', '')} {getattr(standard, 'version', '')}".lower()
    if re.search(r'(?<!\d)27001(?!\d)', name_ver):
        return PROFILES['iso27001-2022']
    if _NON_ISO_STANDARD_RE.search(name_ver):
        return PROFILES['generic']
    for control in getattr(standard, 'controls', None) or ():
        cid = str(control.get('id', '')).strip() if isinstance(control, dict) else ''
        if _ISO_ANNEX_CONTROL_RE.match(cid):
            return PROFILES['iso27001-2022']
    return PROFILES['generic']


def standard_label(standard, profile: StandardProfile | None = None) -> str:
    profile = profile or resolve(standard)
    if profile.display_name:
        return profile.display_name
    if standard is None:
        return 'the selected compliance standard'
    name = str(getattr(standard, 'name', '') or '').strip()
    version = str(getattr(standard, 'version', '') or '').strip()
    if name and version and version.lower() not in name.lower():
        return f'{name} {version}'
    return name or version or 'the selected compliance standard'


def control_rules(profile: StandardProfile, control_id: str) -> str:
    return profile.clause_rules.get(control_id, '')


def standard_metadata(standard, profile: StandardProfile | None = None) -> dict:
    profile = profile or resolve(standard)
    return {
        'profile_id': profile.id,
        'name': standard_label(standard, profile),
        'guidance_name': profile.guidance_name,
        'system_term': profile.system_term,
    }


def profile_snapshot(standard, profile: StandardProfile | None = None) -> dict:
    profile = profile or resolve(standard)
    rules_digest = sha256(
        json.dumps(profile.clause_rules, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    return {
        'id': profile.id,
        'version': profile.version,
        'rules_sha256': rules_digest,
    }
