"""Store independent coverage checks for matching assessment inputs."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'note_coverage_checks',
        sa.Column('assessment_id', sa.String(36), sa.ForeignKey('assessments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('control_id', sa.String(200), primary_key=True),
        sa.Column('mode', sa.String(20), primary_key=True),
        sa.Column('fingerprint', sa.String(64), primary_key=True),
        sa.Column('missing_details', sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table('note_coverage_checks')
