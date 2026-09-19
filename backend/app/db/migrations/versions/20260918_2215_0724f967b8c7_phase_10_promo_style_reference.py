"""phase 10 promo style reference

Revision ID: 0724f967b8c7
Revises: 1e938c9563fd
Create Date: 2026-09-18 22:15:27.441401

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0724f967b8c7'
down_revision = '1e938c9563fd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same pattern as the other Phase 9/10 columns: add with a server default so the
    # existing rows get a value, then drop the default so the schema matches the model
    # and future autogenerate runs report no drift.
    op.add_column(
        'readme_promos',
        sa.Column('style_reference', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column('readme_promos', 'style_reference', server_default=None)


def downgrade() -> None:
    op.drop_column('readme_promos', 'style_reference')
