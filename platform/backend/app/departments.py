"""Assessment-local responsibility suggestions; never rewrite a standard.

Suggestions use explicit template/question ownership first, then a small,
transparent topic/role vocabulary. They are not LLM decisions or compliance
findings. Unknown ownership stays unassigned until a user selects it.
"""
import re
from .questionnaire import control_ids

UNASSIGNED = 'รอเลือกแผนก'
GENERIC = {'all', 'all departments', 'ทุกแผนก', 'ทั้งหมด', '', UNASSIGNED.casefold()}
ROLES = {
    'it': (('it', 'ict', 'technology', 'technical', 'engineering', 'ไอที', 'เทคโนโลยี'),
           ('access', 'account', 'authentication', 'network', 'software', 'source code', 'backup', 'restoration', 'logging', 'cryptograph', 'malware', 'vulnerabil', 'cloud', 'system development', 'configuration', 'สิทธิ์', 'เครือข่าย', 'สำรองข้อมูล')),
    'hr': (('hr', 'human resources', 'people', 'บุคคล', 'ทรัพยากรบุคคล'),
           ('employee', 'personnel', 'recruit', 'employment', 'staff', 'training', 'awareness', 'disciplinary', 'termination', 'พนักงาน', 'อบรม', 'บุคลากร')),
    'finance': (('finance', 'accounting', 'บัญชี', 'การเงิน'),
                ('invoice', 'payment', 'financial', 'budget', 'accounting', 'ใบแจ้งหนี้', 'การเงิน', 'งบประมาณ')),
    'legal': (('legal', 'compliance', 'privacy', 'กฎหมาย', 'กำกับ', 'คุ้มครองข้อมูล'),
              ('legal', 'regulat', 'legislat', 'privacy', 'intellectual property', 'contractual', 'กฎหมาย', 'ข้อบังคับ', 'ความเป็นส่วนตัว')),
    'procurement': (('procurement', 'purchasing', 'sourcing', 'จัดซื้อ'),
                    ('supplier', 'supply chain', 'vendor', 'procurement', 'purchas', 'ผู้ขาย', 'ผู้ให้บริการ', 'จัดซื้อ')),
    'facilities': (('facilities', 'physical security', 'building', 'อาคาร', 'สถานที่'),
                   ('physical', 'premises', 'perimeter', 'cabling', 'utilities', 'equipment siting', 'secure areas', 'อาคาร', 'สถานที่', 'กายภาพ')),
    'security': (('security', 'cybersecurity', 'risk', 'ความมั่นคง', 'ความปลอดภัย', 'ความเสี่ยง'),
                 ('security policy', 'information security', 'incident', 'risk', 'threat', 'intelligence', 'security review', 'ความเสี่ยง', 'เหตุการณ์', 'ความมั่นคงปลอดภัย')),
    'management': (('management', 'executive', 'leadership', 'board', 'บริหาร'),
                   ('leadership', 'top management', 'security objectives', 'management review', 'governance', 'scope', 'interested parties', 'continual improvement', 'ผู้บริหาร', 'วัตถุประสงค์')),
    'operations': (('operations', 'operation', 'business continuity', 'ปฏิบัติการ', 'ดำเนินงาน'),
                   ('operational', 'business continuity', 'disruption', 'business process', 'ปฏิบัติการ', 'ความต่อเนื่อง')),
}


def matches(text, word):
    # Short role acronyms must not match inside words such as "audit".
    if word.isascii() and len(word) <= 3:
        return bool(re.search(r'(?<![a-z0-9])'+re.escape(word)+r'(?![a-z0-9])', text))
    return word in text


def company_departments(context):
    return list(dict.fromkeys(d.strip() for d in context.get('departments', [])
                             if isinstance(d, str) and d.strip() and d.strip().casefold() not in GENERIC))


def assignments(controls, context, questions=(), answers=None):
    from .answer_departments import current_entry
    names = company_departments(context)
    canonical = {d.casefold():d for d in names}
    overrides = context.get('department_overrides', {})
    proposed = {}
    for q in questions:
        if q.get('source') == 'user' or len(control_ids(q)) != 1:
            # A shared interview's department union is not each control's owner.
            continue
        for cid in control_ids(q):
            proposed.setdefault(cid, []).extend(q.get('departments') or [q.get('department')])
    def allowed(values):
        return list(dict.fromkeys(canonical[v.strip().casefold()] for v in values
                                  if isinstance(v, str) and v.strip().casefold() in canonical))
    result = {}
    for c in controls:
        cid = c['id']
        if cid in overrides:
            values, source = allowed(overrides[cid]), 'manual'
        elif answers is not None and (entry := current_entry(c,answers.get(cid,{}),context,questions)) is not None:
            values, source = allowed(entry['departments']), ('single_department' if entry.get('provider')=='company_single_department' else 'ai_answer')
        elif allowed(proposed.get(cid, [])):
            values, source = allowed(proposed[cid]), 'question'
        elif allowed([c.get('department', '')]):
            values, source = allowed([c['department']]), 'template'
        else:
            topic = c['requirement'].casefold()
            template = c.get('department', '').casefold()
            values = []
            for name in names:
                normalized = name.casefold()
                roles = [role for role, (aliases, _) in ROLES.items() if any(matches(normalized, a) for a in aliases)]
                if any(any(matches(topic, term) for term in ROLES[role][1])
                       or any(matches(template, alias) for alias in ROLES[role][0]) for role in roles):
                    values.append(name)
            source = 'suggested' if values else 'unassigned'
            if not values and len(names) == 1:
                values, source = names[:], 'single_department'
        result[cid] = {'departments':values, 'source':source}
        if source == 'ai_answer':result[cid]['reason'] = entry['reason']
    return result


def scoped_controls(controls, routing):
    return [{**c, 'departments':routing[c['id']]['departments'],
             'department':' / '.join(routing[c['id']]['departments']) or UNASSIGNED} for c in controls]


def scoped_questions(questions, routing):
    result = []
    for q in questions:
        if q.get('source') == 'user':
            result.append(dict(q))
            continue
        names = list(dict.fromkeys(d for cid in control_ids(q) if cid in routing for d in routing[cid]['departments']))
        result.append({**q, 'departments':names or [UNASSIGNED], 'department':names[0] if names else UNASSIGNED})
    return result
