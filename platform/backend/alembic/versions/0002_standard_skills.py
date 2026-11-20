"""Standard-scoped skill families with immutable core and append-only versions."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('skill_families',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('standard_id', sa.String(36), sa.ForeignKey('standards.id'), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('core_instructions', sa.String, nullable=False),
        sa.UniqueConstraint('standard_id', 'name'))
    op.create_table('standard_skill_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('family_id', sa.String(36), sa.ForeignKey('skill_families.id'), nullable=False),
        sa.Column('parent_id', sa.String(36), sa.ForeignKey('standard_skill_versions.id'), nullable=True),
        sa.Column('version', sa.String(100), nullable=False),
        sa.Column('instructions', sa.String, nullable=False),
        sa.Column('change_note', sa.String(4000), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('family_id', 'version'))

def downgrade():
    op.drop_table('standard_skill_versions')
    op.drop_table('skill_families')
