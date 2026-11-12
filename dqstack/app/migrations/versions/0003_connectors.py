"""Connectors: Tally (files) and Zoho Inventory (API). The connector itself with its
encrypted credentials and sync cursor, the log of every sync, and the last version
seen of each Zoho document.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('connectors',
    sa.Column('company_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('credentials', sa.LargeBinary(), nullable=True),
    sa.Column('cursor', sa.JSON(), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'kind')
    )
    with op.batch_alter_table('connectors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connectors_company_id'), ['company_id'], unique=False)

    op.create_table('connector_docs',
    sa.Column('company_id', sa.String(length=32), nullable=False),
    sa.Column('connector_id', sa.String(length=32), nullable=False),
    sa.Column('doc_kind', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=False),
    sa.Column('day', sa.Date(), nullable=True),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('modified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['connector_id'], ['connectors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('connector_id', 'doc_kind', 'external_id')
    )
    with op.batch_alter_table('connector_docs', schema=None) as batch_op:
        batch_op.create_index('ix_connector_doc_day', ['connector_id', 'doc_kind', 'day'], unique=False)
        batch_op.create_index(batch_op.f('ix_connector_docs_company_id'), ['company_id'], unique=False)

    op.create_table('connector_runs',
    sa.Column('company_id', sa.String(length=32), nullable=False),
    sa.Column('connector_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('import_job_ids', sa.JSON(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('person_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['connector_id'], ['connectors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('connector_runs', schema=None) as batch_op:
        batch_op.create_index('ix_connector_run_company_started', ['company_id', 'started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_connector_runs_company_id'), ['company_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_connector_runs_connector_id'), ['connector_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('connector_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connector_runs_connector_id'))
        batch_op.drop_index(batch_op.f('ix_connector_runs_company_id'))
        batch_op.drop_index('ix_connector_run_company_started')
    op.drop_table('connector_runs')

    with op.batch_alter_table('connector_docs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connector_docs_company_id'))
        batch_op.drop_index('ix_connector_doc_day')
    op.drop_table('connector_docs')

    with op.batch_alter_table('connectors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connectors_company_id'))
    op.drop_table('connectors')
