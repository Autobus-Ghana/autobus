"""Add AI Workflow tables

Revision ID: 20261013_add_ai_workflow_tables
Revises: None
Create Date: 2026-10-13 11:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261013_add_ai_workflow_tables'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = []
    try:
        inspector = sa.inspect(bind)
        existing_tables = inspector.get_table_names()
    except Exception:
        # In offline/mock mode or if inspector isn't available, proceed with generation
        existing_tables = []

    # 1. Create ai_workflow_businesses table if not present
    if 'ai_workflow_businesses' not in existing_tables:
        op.create_table(
            'ai_workflow_businesses',
            sa.Column('id', sa.String(length=64), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_ai_workflow_businesses_name'), 'ai_workflow_businesses', ['name'], unique=True)

    # 2. Create ai_workflows table if not present
    if 'ai_workflows' not in existing_tables:
        op.create_table(
            'ai_workflows',
            sa.Column('id', sa.String(length=64), nullable=False),
            sa.Column('name', sa.String(length=200), nullable=False),
            sa.Column('business_name', sa.String(length=120), nullable=False),
            sa.Column('business_id', sa.String(length=64), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('category', sa.String(length=60), nullable=False, server_default='General'),
            sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('nodes', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
            sa.Column('edges', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
            sa.Column('n8n_id', sa.String(length=120), nullable=True),
            sa.Column('stats', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['business_id'], ['ai_workflow_businesses.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_ai_workflows_name'), 'ai_workflows', ['name'], unique=False)
        op.create_index(op.f('ix_ai_workflows_business_name'), 'ai_workflows', ['business_name'], unique=False)
        op.create_index(op.f('ix_ai_workflows_business_id'), 'ai_workflows', ['business_id'], unique=False)
        op.create_index('ix_ai_workflows_category', 'ai_workflows', ['category'], unique=False)
        op.create_index('ix_ai_workflows_business_active', 'ai_workflows', ['business_name', 'active'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = []
    try:
        inspector = sa.inspect(bind)
        existing_tables = inspector.get_table_names()
    except Exception:
        existing_tables = ['ai_workflows', 'ai_workflow_businesses']

    if 'ai_workflows' in existing_tables:
        op.drop_index('ix_ai_workflows_business_active', table_name='ai_workflows')
        op.drop_index('ix_ai_workflows_category', table_name='ai_workflows')
        op.drop_index(op.f('ix_ai_workflows_business_id'), table_name='ai_workflows')
        op.drop_index(op.f('ix_ai_workflows_business_name'), table_name='ai_workflows')
        op.drop_index(op.f('ix_ai_workflows_name'), table_name='ai_workflows')
        op.drop_table('ai_workflows')

    if 'ai_workflow_businesses' in existing_tables:
        op.drop_index(op.f('ix_ai_workflow_businesses_name'), table_name='ai_workflow_businesses')
        op.drop_table('ai_workflow_businesses')
