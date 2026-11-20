from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

def safe_strings(value):
    if isinstance(value, str) and any((ord(c) < 32 and c not in '\t\n\r') or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in {0xFFFE,0xFFFF} for c in value):
        raise ValueError('Text contains unsupported control characters')
    if isinstance(value, dict):
        for k,v in value.items():
            safe_strings(k)
            safe_strings(v)
    if isinstance(value, list):
        for v in value:
            safe_strings(v)
    return value

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @field_validator('*', mode='before')
    @classmethod
    def safe_text(cls, value):
        return safe_strings(value)

class Context(Strict):
    company: str = Field(min_length=1, max_length=200)
    business_type: str = Field(min_length=1, max_length=200)
    departments: list[str] = Field(default_factory=list, max_length=30)
    it_systems: list[str] = Field(default_factory=list)
    data_types: list[str] = Field(default_factory=list)
    existing_policies: list[str] = Field(default_factory=list)
    question_language: Literal['th', 'en'] = 'en'
    auditor: str = Field(default='', max_length=500)
    observer: str = Field(default='', max_length=500)

class CreateAssessment(Strict):
    name: str = Field(min_length=1, max_length=200)
    standard_id: str
    context: Context
    skill_id: str | None = None
    generation_id: UUID | None = None
    question_provider: Literal['checklist', 'inference', 'codex'] = 'checklist'
    authorize_external: bool = False

    @model_validator(mode='after')
    def generation_departments(self):
        if self.question_provider != 'checklist' and not self.context.departments:
            raise ValueError('Departments are required for AI question generation')
        return self


class DeleteAssessmentInput(Strict):
    expected_version: int = Field(ge=1)
    confirm_name: str = Field(min_length=1, max_length=200)


class ChecklistSetupInput(Strict):
    expected_version: int = Field(ge=1)
    departments: list[str] = Field(min_length=1, max_length=30)
    auditor: str = Field(default='', max_length=500)
    observer: str = Field(default='', max_length=500)

    @field_validator('departments')
    @classmethod
    def department_names(cls, values):
        names = [v.strip() for v in values]
        if any(not v or len(v) > 200 for v in names) or len(set(names)) != len(names):
            raise ValueError('Department names must be nonempty, unique, and at most 200 characters')
        return names

class Answer(Strict):
    status: Literal['unanswered', 'yes', 'partial', 'no', 'na'] = 'unanswered'
    text: str = Field(default='', max_length=20000)

class ReplaceAnswers(Strict):
    expected_version: int = Field(ge=1)
    answers: dict[str, Answer]


class DepartmentAssignments(Strict):
    expected_version: int = Field(ge=1)
    assignments: dict[str, list[str]] = Field(min_length=1, max_length=500)

class PolicyInput(Strict):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=200000)
    control_ids: list[str] = Field(min_length=1)

class SkillInput(Strict):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20000)
    standard_id: str | None = None
    core_instructions: str | None = Field(default=None, min_length=1, max_length=20000)

class SkillVersionInput(Strict):
    version: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20000)
    change_note: str = Field(min_length=1, max_length=4000)

class RunInput(Strict):
    processing_mode: Literal['colab','codex'] | None = None
    authorize_external: bool = False
    job_id: str | None = Field(default=None, max_length=36)
    skill_id: str | None = None
    full_reassessment: bool = False


class AutoDepartmentsInput(Strict):
    expected_version: int = Field(ge=1)
    force: bool = False


class AnswerDepartmentMapping(Strict):
    control_id: str = Field(min_length=1,max_length=200)
    departments: list[str] = Field(max_length=30)
    reason: str = Field(min_length=1,max_length=2000)

class AuditClassification(Strict):
    category: Literal['comply', 'ofi', 'nc_minor', 'nc_major', 'needs_review', 'not_applicable']
    reason: str = Field(min_length=1, max_length=4000)
    requirement_gap: str = Field(default='', max_length=4000)
    system_impact: Literal['affects', 'does_not_affect', 'unknown'] = 'unknown'


class QuestionFinding(Strict):
    question_id: str = Field(min_length=1, max_length=300)
    question_text: str = Field(min_length=1, max_length=20000)
    status: Literal['met', 'gap', 'partial', 'needs_review', 'not_applicable']
    rationale: str = Field(max_length=4000)
    recommendation: str = Field(default='', max_length=2000)
    audit: AuditClassification
    evidence_ids: list[str] = Field(default_factory=list, max_length=200)


class Finding(Strict):
    control_id: str
    status: Literal['met', 'gap', 'partial', 'needs_review', 'not_applicable']
    rationale: str = Field(max_length=20000)
    recommendation: str = Field(max_length=20000)
    evidence_ids: list[str]
    interpreted_answer: str = Field(default='', max_length=20000)
    audit: AuditClassification | None = None
    question_findings: list[QuestionFinding] = Field(default_factory=list, max_length=200)

class MappingInput(Strict):
    text: str = Field(min_length=1, max_length=20000)

class MapAnswersInput(Strict):
    text: str = Field(min_length=1, max_length=50000)

class NoteCoverageTopic(Strict):
    topic: str
    status: Literal['covered', 'partial', 'missing']
    quote: str
    source_control_id: str
    missing_detail: str

class NoteCoverageResult(Strict):
    summary: str
    topics: list[NoteCoverageTopic]

class NoteReviewInput(Strict):
    expected_version: int = Field(ge=1)
    control_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20000)
    processing_mode: Literal['colab','codex'] = 'colab'
    authorize_external: bool = False


class QuestionMappingInput(Strict):
    expected_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    answer_text: str = Field(default='', max_length=20000)
    departments: list[str] = Field(default_factory=list, max_length=30)
    skill_id: str | None = None
    authorize_external: bool = False


class QuestionControlMapping(Strict):
    control_id: str = Field(min_length=1, max_length=200)
    score: float = Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)


class AddQuestionInput(Strict):
    expected_version: int = Field(ge=1)
    preview_id: str = Field(min_length=1, max_length=36)
    control_ids: list[str] = Field(min_length=1, max_length=200)

class AddControlQuestionInput(Strict):
    expected_version: int = Field(ge=1)
    control_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000)
    answer_text: str = Field(default='', max_length=20000)

class AnswerMapping(Strict):
    control_id: str
    source_texts: list[str] = Field(min_length=1, max_length=100)
    answer_text: str = Field(min_length=1, max_length=20000)
    suggested_status: Literal['yes','partial','no','na','unanswered']
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=4000)

class UnmatchedAnswer(Strict):
    source_text: str = Field(min_length=1, max_length=20000)
    reason: str = Field(min_length=1, max_length=4000)

class MapAnswersResult(Strict):
    mappings: list[AnswerMapping]
    unmatched: list[UnmatchedAnswer]


class ProcessingModeInput(Strict):
    expected_version: int = Field(ge=1)
    mode: Literal['colab','codex']

class FeedbackInput(Strict):
    run_id: str
    run_mode: Literal['colab','codex']
    control_id: str
    category: Literal['comply','ofi','nc_minor','nc_major','needs_review','not_applicable']
    explanation: str = Field(min_length=1,max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list,max_length=100)

class FeedbackReviewInput(Strict):
    decision: Literal['approved','rejected']
    reviewer: str = Field(min_length=1,max_length=200)
