"""Sign-in extras: one-time codes (password reset by phone, two-step sign-in) and the
company's sign-in security setting.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('signin', sa.JSON(), nullable=True))

    op.create_table('one_time_codes',
    sa.Column('company_id', sa.String(length=32), nullable=True),
    sa.Column('person_id', sa.String(length=32), nullable=False),
    sa.Column('purpose', sa.String(length=16), nullable=False),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('ticket_hash', sa.String(length=64), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('sends', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ticket_hash')
    )
    with op.batch_alter_table('one_time_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_one_time_codes_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_one_time_codes_person_id'), ['person_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('one_time_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_one_time_codes_person_id'))
        batch_op.drop_index(batch_op.f('ix_one_time_codes_company_id'))

    op.drop_table('one_time_codes')
    # A plain ALTER TABLE ... DROP COLUMN (SQLite 3.35+ and PostgreSQL), not batch mode:
    # batch mode would rebuild `companies`, and dropping the old copy with foreign keys
    # on would cascade into every table that points at it.
    op.drop_column('companies', 'signin')
