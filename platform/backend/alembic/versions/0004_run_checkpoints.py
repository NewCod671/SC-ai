"""Durable successful batches for safe resumption, separate from final runs."""
from alembic import op
import sqlalchemy as sa
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('run_checkpoints',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('assessment_id',sa.String(36),sa.ForeignKey('assessments.id'),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('provider',sa.String(200),nullable=False),
        sa.Column('skill',sa.JSON(),nullable=False),
        sa.Column('results',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_run_checkpoints_assessment_id','run_checkpoints',['assessment_id'])


def downgrade():
    op.drop_index('ix_run_checkpoints_assessment_id',table_name='run_checkpoints')
    op.drop_table('run_checkpoints')
