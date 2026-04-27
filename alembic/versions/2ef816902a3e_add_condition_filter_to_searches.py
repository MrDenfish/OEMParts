"""Add condition_filter to searches

Revision ID: 2ef816902a3e
Revises: 862710ad10dd
Create Date: 2026-04-26 21:52:47.490151

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ef816902a3e'
down_revision: Union[str, Sequence[str], None] = '862710ad10dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('searches', sa.Column('condition_filter', sa.String(length=20), nullable=True, comment='New, Used, or NULL for any'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('searches', 'condition_filter')
