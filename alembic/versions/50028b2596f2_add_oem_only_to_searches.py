"""add oem_only to searches

Revision ID: 50028b2596f2
Revises: 2ef816902a3e
Create Date: 2026-04-28 09:11:38.128541

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50028b2596f2'
down_revision: Union[str, Sequence[str], None] = '2ef816902a3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'searches',
        sa.Column(
            'oem_only',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="When true and oem_number is set, drop listings whose title doesn't contain OEM, Genuine, or the OEM number",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('searches', 'oem_only')
