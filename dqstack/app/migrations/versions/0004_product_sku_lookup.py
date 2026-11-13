"""An index on upper(sku), so tills and imports find a product by SKU in one lookup.

The POS API, Checkout and imports match SKUs without regard to case. The unique
(company_id, sku) index cannot serve upper(sku) = ..., so each bill line scanned every
product of the company. PostgreSQL and SQLite both index expressions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_products_company_upper_sku', 'products',
                    ['company_id', sa.text('upper(sku)')])


def downgrade() -> None:
    op.drop_index('ix_products_company_upper_sku', table_name='products')
