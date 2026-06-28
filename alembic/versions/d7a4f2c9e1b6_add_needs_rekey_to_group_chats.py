"""add needs_rekey flag to group_chats

Revision ID: d7a4f2c9e1b6
Revises: e2b9d7c5a3f1
Create Date: 2026-06-27 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a4f2c9e1b6'
down_revision: Union[str, Sequence[str], None] = 'e2b9d7c5a3f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # "dirty"-Flag für Lazy Rekeying. server_default='false', damit bestehende Gruppen
    # einen definierten Wert bekommen (nicht NULL).
    op.add_column('group_chats', sa.Column('needs_rekey', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('group_chats', 'needs_rekey')
