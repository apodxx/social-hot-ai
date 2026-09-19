"""phase 9 image layout plan

Revision ID: 054433adad83
Revises: 667744599744
Create Date: 2026-09-18 20:26:49.686146

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '054433adad83'
down_revision = '667744599744'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same pattern as the media migration: add with a server default so the 8 existing
    # rewrite rows get a value, then drop the default so the schema matches the model
    # and future autogenerate runs stay clean.
    op.add_column(
        'ai_rewrites',
        sa.Column('layout', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column('ai_rewrites', 'layout', server_default=None)


def downgrade() -> None:
    op.drop_column('ai_rewrites', 'layout')
