from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, JSON, ForeignKey, Integer, LargeBinary, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def uid():
    return str(uuid4())

class Standard(Base):
    __tablename__ = 'standards'
    __table_args__ = (UniqueConstraint('name', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    controls: Mapped[list] = mapped_column(JSON)
    mapping: Mapped[dict] = mapped_column(JSON)
    template: Mapped[bytes] = mapped_column(LargeBinary)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)

class Assessment(Base):
    __tablename__ = 'assessments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    standard_id: Mapped[str] = mapped_column(ForeignKey('standards.id'))
    context: Mapped[dict] = mapped_column(JSON)
    questions: Mapped[list] = mapped_column(JSON)
    current_version: Mapped[int] = mapped_column(Integer, default=1)

class Revision(Base):
    __tablename__ = 'revisions'
    __table_args__ = (UniqueConstraint('assessment_id', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'))
    version: Mapped[int] = mapped_column(Integer)
    answers: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Evidence(Base):
    __tablename__ = 'evidence'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'))
    control_id: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    text: Mapped[str] = mapped_column(String)

class Policy(Base):
    __tablename__ = 'policies'
    __table_args__ = (UniqueConstraint('assessment_id', 'name', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(String)
    control_ids: Mapped[list] = mapped_column(JSON)
    changes: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Skill(Base):
    __tablename__ = 'skills'
    __table_args__ = (UniqueConstraint('name', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(String)

class SkillFamily(Base):
    __tablename__ = 'skill_families'
    __table_args__ = (UniqueConstraint('standard_id', 'name'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    standard_id: Mapped[str] = mapped_column(ForeignKey('standards.id'))
    name: Mapped[str] = mapped_column(String(200))
    core_instructions: Mapped[str] = mapped_column(String)

class StandardSkillVersion(Base):
    __tablename__ = 'standard_skill_versions'
    __table_args__ = (UniqueConstraint('family_id', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    family_id: Mapped[str] = mapped_column(ForeignKey('skill_families.id'))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey('standard_skill_versions.id'), nullable=True)
    version: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(String)
    change_note: Mapped[str] = mapped_column(String(4000), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'))
    revision: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(200))
    skill: Mapped[dict] = mapped_column(JSON)
    results: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RunSummary(Base):
    __tablename__ = 'run_summaries'
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(36))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RunCheckpoint(Base):
    """Validated partial findings; never displayed as a completed assessment."""
    __tablename__ = 'run_checkpoints'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(200))
    skill: Mapped[dict] = mapped_column(JSON)
    results: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class AssessmentAISettings(Base):
    __tablename__ = 'assessment_ai_settings'
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default='colab')

class AgentRun(Base):
    __tablename__ = 'agent_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40))
    snapshot: Mapped[dict] = mapped_column(JSON)
    results: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class AuditorFeedback(Base):
    __tablename__ = 'auditor_feedback'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('assessments.id'), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    run_mode: Mapped[str] = mapped_column(String(20))
    control_id: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSON)
    prediction: Mapped[dict] = mapped_column(JSON)
    category: Mapped[str] = mapped_column(String(40))
    explanation: Mapped[str] = mapped_column(String(4000))
    reviewer: Mapped[str] = mapped_column(String(200), default='')
    decision: Mapped[str] = mapped_column(String(20), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
