"""Immutable standard-level summary and rule snapshot for each assessment run."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('run_summaries',
        sa.Column('run_id', sa.String(36), sa.ForeignKey('runs.id'), primary_key=True),
        sa.Column('data', sa.JSON, nullable=False))

def downgrade():
    op.drop_table('run_summaries')
