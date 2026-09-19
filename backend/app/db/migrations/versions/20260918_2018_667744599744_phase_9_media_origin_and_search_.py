"""phase 9: media, origin and search provenance

Revision ID: 667744599744
Revises: 09191b9cb921
Create Date: 2026-09-18 20:18:08.949832

Autogenerate emitted three ``NOT NULL`` columns with no default, which fails against a
table that already holds rows (298 at the time of writing). Each one is therefore added
*with* a server default so existing rows get a value, and the default is then dropped so
the schema matches the model declaration exactly — leaving it in place would make every
future ``--autogenerate`` report a spurious server-default drift.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '667744599744'
down_revision = '09191b9cb921'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows are ranking entries: no media, first discovered from the hot list.
    op.add_column(
        'hot_contents',
        sa.Column('media', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        'hot_contents',
        sa.Column('image_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'hot_contents',
        sa.Column('origin', sa.String(length=16), nullable=False, server_default='hot'),
    )
    op.add_column(
        'hot_contents',
        sa.Column('source_keyword', sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f('ix_hot_contents_origin'), 'hot_contents', ['origin'], unique=False
    )
    op.create_index(
        op.f('ix_hot_contents_source_keyword'),
        'hot_contents',
        ['source_keyword'],
        unique=False,
    )

    for column in ('media', 'image_count', 'origin'):
        op.alter_column('hot_contents', column, server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_hot_contents_source_keyword'), table_name='hot_contents')
    op.drop_index(op.f('ix_hot_contents_origin'), table_name='hot_contents')
    op.drop_column('hot_contents', 'source_keyword')
    op.drop_column('hot_contents', 'origin')
    op.drop_column('hot_contents', 'image_count')
    op.drop_column('hot_contents', 'media')
