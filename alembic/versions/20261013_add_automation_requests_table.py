"""Add automation_requests table

Revision ID: 20261013_add_automation_requests_table
Revises: 20261013_add_ai_workflow_tables
Create Date: 2026-10-13 11:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261013_add_automation_requests_table'
down_revision: Union[str, None] = '20261013_add_ai_workflow_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = []
    try:
        inspector = sa.inspect(bind)
        existing_tables = inspector.get_table_names()
    except Exception:
        existing_tables = []

    if 'automation_requests' not in existing_tables:
        op.create_table(
            'automation_requests',
            sa.Column('id', sa.String(length=64), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('description', sa.Text(), nullable=False),
            sa.Column('category', sa.String(length=60), nullable=False, server_default='Other'),
            sa.Column('priority', sa.String(length=20), nullable=False, server_default='Normal'),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='Submitted'),
            sa.Column('requested_by', sa.String(length=150), nullable=False, server_default='Merchant'),
            sa.Column('business_name', sa.String(length=150), nullable=True),
            sa.Column('contact_email', sa.String(length=150), nullable=True),
            sa.Column('contact_phone', sa.String(length=50), nullable=True),
            sa.Column('assigned_to', sa.String(length=150), nullable=True),
            sa.Column('workflow_id', sa.String(length=64), nullable=True),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('survey_data', sa.JSON(), nullable=True, server_default=sa.text("'{}'::json")),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['workflow_id'], ['ai_workflows.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_automation_requests_title'), 'automation_requests', ['title'], unique=False)
        op.create_index(op.f('ix_automation_requests_category'), 'automation_requests', ['category'], unique=False)
        op.create_index(op.f('ix_automation_requests_status'), 'automation_requests', ['status'], unique=False)
        op.create_index(op.f('ix_automation_requests_business_name'), 'automation_requests', ['business_name'], unique=False)
        op.create_index(op.f('ix_automation_requests_created_at'), 'automation_requests', ['created_at'], unique=False)
        op.create_index('ix_automation_requests_status_category', 'automation_requests', ['status', 'category'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = []
    try:
        inspector = sa.inspect(bind)
        existing_tables = inspector.get_table_names()
    except Exception:
        existing_tables = ['automation_requests']

    if 'automation_requests' in existing_tables:
        op.drop_index('ix_automation_requests_status_category', table_name='automation_requests')
        op.drop_index(op.f('ix_automation_requests_created_at'), table_name='automation_requests')
        op.drop_index(op.f('ix_automation_requests_business_name'), table_name='automation_requests')
        op.drop_index(op.f('ix_automation_requests_status'), table_name='automation_requests')
        op.drop_index(op.f('ix_automation_requests_category'), table_name='automation_requests')
        op.drop_index(op.f('ix_automation_requests_title'), table_name='automation_requests')
        op.drop_table('automation_requests')
