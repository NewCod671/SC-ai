"""Initial relational schema; intentionally frozen independently of app models."""
from alembic import op
import sqlalchemy as sa
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

def pk():
    return sa.Column('id', sa.String(36), primary_key=True)

def assessment_fk():
    return sa.Column('assessment_id', sa.String(36), sa.ForeignKey('assessments.id'), nullable=False)

def upgrade():
    op.create_table('standards', pk(), sa.Column('name', sa.String(200), nullable=False), sa.Column('version', sa.String(100), nullable=False), sa.Column('controls', sa.JSON, nullable=False), sa.Column('mapping', sa.JSON, nullable=False), sa.Column('template', sa.LargeBinary, nullable=False), sa.Column('changes', sa.JSON, nullable=False), sa.UniqueConstraint('name','version'))
    op.create_table('assessments', pk(), sa.Column('name', sa.String(200), nullable=False), sa.Column('standard_id', sa.String(36), sa.ForeignKey('standards.id'), nullable=False), sa.Column('context', sa.JSON, nullable=False), sa.Column('questions', sa.JSON, nullable=False), sa.Column('current_version', sa.Integer, nullable=False))
    op.create_table('revisions', pk(), assessment_fk(), sa.Column('version', sa.Integer, nullable=False), sa.Column('answers', sa.JSON, nullable=False), sa.Column('source', sa.String(40), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint('assessment_id','version'))
    op.create_table('evidence', pk(), assessment_fk(), sa.Column('control_id', sa.String(200), nullable=False), sa.Column('filename', sa.String(255), nullable=False), sa.Column('sha256', sa.String(64), nullable=False), sa.Column('content', sa.LargeBinary, nullable=False), sa.Column('text', sa.String, nullable=False))
    op.create_table('policies', pk(), assessment_fk(), sa.Column('name', sa.String(200), nullable=False), sa.Column('version', sa.String(100), nullable=False), sa.Column('text', sa.String, nullable=False), sa.Column('control_ids', sa.JSON, nullable=False), sa.Column('changes', sa.JSON, nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint('assessment_id','name','version'))
    op.create_table('skills', pk(), sa.Column('name', sa.String(200), nullable=False), sa.Column('version', sa.String(100), nullable=False), sa.Column('instructions', sa.String, nullable=False), sa.UniqueConstraint('name','version'))
    op.create_table('runs', pk(), assessment_fk(), sa.Column('revision', sa.Integer, nullable=False), sa.Column('provider', sa.String(200), nullable=False), sa.Column('skill', sa.JSON, nullable=False), sa.Column('results', sa.JSON, nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('audit', pk(), sa.Column('action', sa.String(100), nullable=False), sa.Column('entity_id', sa.String(36), nullable=False), sa.Column('details', sa.JSON, nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))

def downgrade():
    for table in ('audit','runs','skills','policies','evidence','revisions','assessments','standards'):
        op.drop_table(table)
