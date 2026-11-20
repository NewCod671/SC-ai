"""Add isolated experimental runs, per-assessment provider and reviewed feedback."""
from alembic import op
import sqlalchemy as sa
revision='0005'
down_revision='0004'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('assessment_ai_settings',sa.Column('assessment_id',sa.String(36),sa.ForeignKey('assessments.id'),primary_key=True),sa.Column('mode',sa.String(20),nullable=False))
    op.create_table('agent_runs',sa.Column('id',sa.String(36),primary_key=True),sa.Column('assessment_id',sa.String(36),sa.ForeignKey('assessments.id'),nullable=False),sa.Column('revision',sa.Integer(),nullable=False),sa.Column('provider',sa.String(200),nullable=False),sa.Column('status',sa.String(40),nullable=False),sa.Column('snapshot',sa.JSON(),nullable=False),sa.Column('results',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_agent_runs_assessment_id','agent_runs',['assessment_id'])
    op.create_table('auditor_feedback',sa.Column('id',sa.String(36),primary_key=True),sa.Column('assessment_id',sa.String(36),sa.ForeignKey('assessments.id'),nullable=False),sa.Column('run_id',sa.String(36),nullable=False),sa.Column('run_mode',sa.String(20),nullable=False),sa.Column('control_id',sa.String(200),nullable=False),sa.Column('provider',sa.String(200),nullable=False),sa.Column('snapshot',sa.JSON(),nullable=False),sa.Column('prediction',sa.JSON(),nullable=False),sa.Column('category',sa.String(40),nullable=False),sa.Column('explanation',sa.String(4000),nullable=False),sa.Column('reviewer',sa.String(200),nullable=False),sa.Column('decision',sa.String(20),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_auditor_feedback_assessment_id','auditor_feedback',['assessment_id'])


def downgrade():
    op.drop_index('ix_auditor_feedback_assessment_id',table_name='auditor_feedback');op.drop_table('auditor_feedback')
    op.drop_index('ix_agent_runs_assessment_id',table_name='agent_runs');op.drop_table('agent_runs')
    op.drop_table('assessment_ai_settings')
