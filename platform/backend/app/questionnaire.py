"""Grouped interview questions; controls remain the unit of answers and findings."""
ASSESSMENT_UNIT_VERSION = 'control-workbook-style-v5'
import hashlib
import re
from collections import Counter
from copy import deepcopy


class QuestionValidationError(ValueError):
    """Content-free code plus existing message; never include model values."""
    def __init__(self, code, message, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(message)


QUESTION_VALIDATION_HINTS = {
    'question_text_format_invalid': 'Return labeled Q lines for every supplied question_index exactly once; use WHY for a shared question.',
    'question_array_invalid': 'Return a nonempty JSON array with no more questions than supplied controls.',
    'question_object_invalid': 'Each question must be a JSON object with control_ids, departments, text and mapping_reason.',
    'question_control_reference_invalid': 'Use only exact supplied control IDs as a nonempty array of unique strings.',
    'question_primary_control_invalid': 'Omit legacy control_id, or make it equal to the first item in control_ids.',
    'question_text_invalid': 'Every question must have a nonempty concise text string.',
    'question_text_empty': 'Write an actual interview question, without system/data inventories or template appendices.',
    'question_departments_invalid': 'departments must be a nonempty array of department-name strings, not a string, object or null.',
    'question_mapping_reason_invalid': 'mapping_reason must be a short string; explain the shared process when a question covers multiple controls.',
    'question_coverage_invalid': 'Every supplied control ID must appear exactly once across all question control_ids. Do not omit, repeat or invent IDs.',
}


def control_ids(question):
    return question.get('control_ids', [question.get('control_id')])


def responsible_departments(control):
    values = control.get('departments', [control.get('department')])
    return [d for d in values if isinstance(d, str) and d.strip()
            and d.strip().casefold() not in {'all','all departments','ทุกแผนก','ทั้งหมด'}]


def clean_question_text(text):
    """Remove only known legacy template appendices, without rewriting questions."""
    text = re.sub(r'(?:^|\s+)(?:Consider systems:|Reference existing policies:)[\s\S]*$', '', text,
                  flags=re.IGNORECASE)
    text = re.sub(r'\s+Describe the owner, process, review frequency and evidence for [^.!?\n]+\.?\s*$', '', text,
                  flags=re.IGNORECASE)
    return text.strip()


def plain_question_output(text, controls):
    """Bind AI-authored wording to one trusted control, never infer new IDs."""
    from .schemas import safe_strings
    if len(controls) != 1 or not isinstance(text, str):
        raise QuestionValidationError('question_text_invalid', 'Expected one plain-text question', 'wrong_type_or_control_count')
    text = text.strip()
    if text.lower().startswith('<think>'):
        end = text.lower().find('</think>')
        if end < 0:
            raise QuestionValidationError('question_text_invalid', 'Unfinished reasoning is not a question', 'unfinished_reasoning')
        text = text[end + len('</think>'):].strip()
    elif '</think>' in text.lower():
        text = re.split(r'</think>', text, maxsplit=1, flags=re.IGNORECASE)[-1].strip()
    reason = ('empty_text' if not text else 'text_too_long' if len(text) > 2000
              else 'too_many_lines' if len(text.splitlines()) > 4
              else 'json_or_markdown_container' if text.startswith(('{', '[', '```'))
              else 'reasoning_marker' if '<think' in text.lower() or '</think' in text.lower() else None)
    if reason:
        raise QuestionValidationError('question_text_invalid', 'Expected concise complete question wording', reason)
    # There is exactly one trusted control: a cosmetic Q8:/1. prefix is not a
    # mapping instruction. Strip only that prefix; preserve the actual wording.
    # Multiple labeled items would be ambiguous, so never collapse them here.
    numbered = r'(?:Q\s*[0-9]+\s*[:.)]|[0-9]+[.)]\s+)'
    if sum(bool(re.match(r'^\s*'+numbered, line, re.IGNORECASE)) for line in text.splitlines()) > 1:
        raise QuestionValidationError('question_text_invalid', 'Expected one question paragraph', 'multiple_numbered_items')
    text = re.sub(r'^'+numbered+r'\s*', '', text, count=1, flags=re.IGNORECASE).strip()
    if re.match(r'^Q\s*[0-9]+\s*,', text, re.IGNORECASE):
        raise QuestionValidationError('question_text_invalid', 'Shared labels are not valid for one control', 'shared_labels_for_single_control')
    safe_strings(text)
    c = controls[0]
    return normalize_questions([{'control_ids':[c['id']], 'departments':responsible_departments(c) or ['รอเลือกแผนก'],
                                 'text':text, 'mapping_reason':''}], controls)


def text_question_batch_output(text, controls):
    """Parse explicit numbered text; control IDs never come from model text.

    Q1,2: shared wording
    WHY: AI explanation of the shared process
    Q3: independent wording
    """
    def invalid(reason):
        raise QuestionValidationError('question_text_format_invalid', 'Incomplete or invalid numbered questions', reason)
    if not isinstance(text, str) or not text.strip() or len(text) > 2000 * len(controls):
        invalid('wrong_type_empty_or_oversized')
    text = text.strip()
    if text.lower().startswith('<think>'):
        end = text.lower().find('</think>')
        if end < 0:
            invalid('unfinished_reasoning')
        text = text[end + len('</think>'):].strip()
    elif '</think>' in text.lower():
        text = re.split(r'</think>', text, maxsplit=1, flags=re.IGNORECASE)[-1].strip()
    if '<think' in text.lower() or '</think' in text.lower() or text.startswith(('{','[','```')):
        invalid('json_or_reasoning_container')
    if len(controls) == 1 and not re.match(r'^Q\d', text, re.IGNORECASE):
        return plain_question_output(text, controls)
    questions, used = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.fullmatch(r'Q([0-9]+(?:\s*,\s*[0-9]+)*)\s*:\s*(.+)', line, re.IGNORECASE)
        if match:
            # Bound digit parsing and reject unknown/repeated labels; never guess missing slots.
            labels = match[1].replace(' ', '').split(',')
            if any(len(label) > 2 for label in labels):
                invalid('invalid_index_length')
            numbers = [int(label) for label in labels]
            if (len(set(numbers)) != len(numbers) or any(n < 1 or n > len(controls) or n in used for n in numbers)):
                invalid('duplicate_or_unknown_index')
            used.update(numbers)
            group = [controls[n-1] for n in numbers]
            questions.append({'control_ids':[c['id'] for c in group],
                              'departments':list(dict.fromkeys(d for c in group for d in responsible_departments(c))) or ['รอเลือกแผนก'],
                              'text':match[2], 'mapping_reason':''})
        elif line.upper().startswith('WHY:') and questions and not questions[-1]['mapping_reason']:
            questions[-1]['mapping_reason'] = line[4:].strip()
        else:
            invalid('unexpected_line')
    if used != set(range(1, len(controls) + 1)):
        invalid('missing_indices')
    from .schemas import safe_strings
    safe_strings(questions)
    return normalize_questions(questions, controls)


def display_questions(questions):
    # Read-time projection: original questionnaire/audit records remain intact.
    return [{**q, 'text': q['text'] if q.get('source') in {'user', 'checklist'} else clean_question_text(q['text'])} for q in questions]


def normalize_questions(result, controls):
    if not isinstance(result, list) or not result or len(result) > len(controls):
        raise QuestionValidationError('question_array_invalid', 'Provider returned invalid questions')
    by_id = {c['id']: c for c in controls}
    covered = []
    normalized = []
    for question in result:
        if not isinstance(question, dict):
            raise QuestionValidationError('question_object_invalid', 'Each question must be an object')
        ids = control_ids(question)
        if (not isinstance(ids, list) or not ids or any(not isinstance(cid, str) or cid not in by_id for cid in ids)
                or len(set(ids)) != len(ids)):
            raise QuestionValidationError('question_control_reference_invalid', 'Question references unknown or duplicate controls')
        if question.get('control_id') is not None and question['control_id'] != ids[0]:
            raise QuestionValidationError('question_primary_control_invalid', 'Legacy primary control must match the first grouped control')
        text = question.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise QuestionValidationError('question_text_invalid', 'Question text must be nonempty and at most 20000 characters')
        text = clean_question_text(text)
        if not text:
            raise QuestionValidationError('question_text_empty', 'Question must contain a question, not just a context appendix')
        departments = question.get('departments', [question.get('department')])
        if (not isinstance(departments, list) or not departments or len(departments) > 100 or
                any(not isinstance(d, str) or not d.strip() or len(d) > 200 for d in departments)):
            raise QuestionValidationError('question_departments_invalid', 'Question departments must be nonempty strings')
        departments = list(dict.fromkeys(d for d in departments + [d for cid in ids for d in responsible_departments(by_id[cid])]
                                        if d.strip().casefold() not in {'all','all departments','ทุกแผนก','ทั้งหมด'})) or ['รอเลือกแผนก']
        reason = question.get('mapping_reason', '')
        if not isinstance(reason, str) or len(reason) > 4000 or (len(ids) > 1 and not reason.strip()):
            raise QuestionValidationError('question_mapping_reason_invalid', 'Shared questions require a mapping reason')
        digest = hashlib.sha256('\0'.join(sorted(ids)).encode()).hexdigest()[:16]
        normalized.append({'id': 'Q-'+digest, 'control_ids': ids, 'departments': departments,
                           'text': text, 'mapping_reason': reason,
                           'control_id': ids[0], 'department': departments[0]})
        covered.extend(ids)
    if Counter(covered) != Counter(by_id.keys()):
        raise QuestionValidationError('question_coverage_invalid', 'Questions must cover every supplied control exactly once')
    return normalized


def text_by_control(questions):
    grouped = {}
    for q in display_questions(questions):
        for cid in control_ids(q):
            grouped.setdefault(cid, []).append(q['text'])
    return {cid: '\n\n'.join(texts) for cid, texts in grouped.items()}


def append_user_answer(answers, ids, question_text, answer_text):
    """Append an interview response without overwriting another control/team.

    Prepare and validate every update before the caller persists the question and
    new answer revision together. Preserve self-reported statuses for review.
    """
    updated = deepcopy(answers)
    if not answer_text.strip():
        return updated
    block = f'คำถามเพิ่มเติม: {question_text}\nคำตอบ: {answer_text.strip()}'
    for cid in ids:
        previous = updated[cid]
        combined = previous['text'] + '\n\n' + block if previous['text'] else block
        if len(combined) > 20000:
            raise ValueError(f'คำตอบของ {cid} ยาวเกิน 20,000 ตัวอักษร กรุณาย่อหรือตรวจคำตอบรายข้อก่อนเพิ่ม')
        previous['text'] = combined
    return updated


def append_control_question_answer(answers, control_id, question_number, question_text, answer_text):
    """Persist an inline question answer in the same numbered format as its page."""
    updated = deepcopy(answers)
    if not answer_text.strip():
        return updated
    block = f'[Q{question_number}] {question_text.replace(chr(10), " ").strip()}\n{answer_text.strip()}'
    previous = updated[control_id]
    combined = previous['text'] + '\n\n' + block if previous['text'] else block
    if len(combined) > 20000:
        raise ValueError(f'คำตอบของ {control_id} ยาวเกิน 20,000 ตัวอักษร กรุณาย่อหรือตรวจคำตอบรายข้อก่อนเพิ่ม')
    previous['text'] = combined
    return updated


_ANSWER_BLOCK = re.compile(r'(?:^|\n\n)\[Q(\d+)\] [^\n]*\n([\s\S]*?)(?=\n\n\[Q\d+\] |$)')
_NAMED_ANSWER_BLOCK = re.compile(r'(?:^|\n\n)คำถามเพิ่มเติม: ([^\n]+)\nคำตอบ:\s*([\s\S]*?)(?=\n\n(?:คำถามเพิ่มเติม: |\[Q\d+\] )|$)')
_ITEM_PREFIX = re.compile(r'^(?:(?:Q|Question)\s*\d+\s*[:.)]|\d{1,2}[.)]|[a-z][.)]|[-*•▪●])\s+', re.I)


def _question_parts(question, control):
    text = question.get('text', '').strip()
    parts = [part.strip() for part in re.split(r'\s*\|\|\s*', text) if part.strip()]
    if question.get('source') == 'user':
        return [text] if text else []
    if question.get('source') == 'checklist':
        if len(parts) > 1:
            return parts
        items, context, current = [], [], ''
        for line in (line.strip() for line in text.splitlines()):
            if not line:
                continue
            match = _ITEM_PREFIX.match(line)
            if match:
                if current:
                    items.append(current)
                current = line[match.end():].strip()
            elif current:
                current += ' ' + line
            else:
                context.append(line)
        if current:
            items.append(current)
        if len(items) > 1:
            if context:
                items[0] = ' '.join(context) + ' ' + items[0]
            return items
        return [text] if text else []
    if 3 <= len(parts) <= 4:
        return parts
    return [text,
        'ใครเป็นผู้รับผิดชอบและมีแผนกใดร่วมดำเนินการในเรื่องนี้?',
        'ปัจจุบันดำเนินการเรื่องนี้อย่างไร ช่วยยกตัวอย่างที่เกิดขึ้นจริงล่าสุด?',
        'ทบทวนผลการดำเนินการเรื่องนี้อย่างไร และมีส่วนใดที่ยังทำไม่ครบหรือควรปรับปรุง?']


def assessment_questions(control, questions, answer):
    """Project the visible subquestions into independent, stable assessment items."""
    cid = control['id']
    related = [q for q in questions if cid in control_ids(q)]
    primary = next((q for q in related if q.get('source') != 'user'), None)
    if primary is None:
        primary = {'id': 'CONTROL-' + cid, 'source': 'checklist', 'text': control.get('requirement', '')}
    sources = [primary, *[q for q in related if q.get('source') == 'user']]
    text = (answer or {}).get('text', '')
    numbered = {int(match.group(1)): match.group(2).strip()
                for match in _ANSWER_BLOCK.finditer(text)}
    named = {match.group(1).strip(): match.group(2).strip()
             for match in _NAMED_ANSWER_BLOCK.finditer(text)}
    prefix = _ANSWER_BLOCK.search(text)
    previous = text[:prefix.start()].strip() if prefix else text.strip()
    items = []
    prompt_index = 0
    for source in sources:
        for part_index, prompt in enumerate(_question_parts(source, control), start=1):
            prompt_index += 1
            response = numbered.get(prompt_index, '')
            if not response and not numbered and source.get('source') == 'user':
                response = named.get(prompt, '')
            if not response and prompt_index == 1 and previous:
                response = previous
            source_id = source.get('id') or 'CONTROL-' + cid
            question_id = f'{source_id}:{part_index}'
            items.append({'question_id': question_id, 'question_text': prompt,
                          'answer_text': response[:20000], 'source': source.get('source', 'checklist')})
    return items
