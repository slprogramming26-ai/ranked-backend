"""remove published column

Revision ID: c0f3f421137a
Revises: f7c2d84a1b93
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0f3f421137a'
down_revision: Union[str, Sequence[str], None] = 'f7c2d84a1b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('posts', 'published')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('posts', sa.Column('published', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True))
