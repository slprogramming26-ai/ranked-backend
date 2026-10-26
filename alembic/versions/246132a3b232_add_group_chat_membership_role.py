"""add group chat membership role

Revision ID: 246132a3b232
Revises: 6354fc5fbbdd
Create Date: 2026-10-26 09:46:00.069579

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '246132a3b232'
down_revision: Union[str, Sequence[str], None] = '6354fc5fbbdd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Bestehende Mitgliedschaften bekommen per server_default automatisch 'member'.
    op.add_column('group_chat_memberships', sa.Column('role', sa.String(), server_default='member', nullable=False))
    op.create_check_constraint(
        'ck_group_chat_memberships_role',
        'group_chat_memberships',
        "role IN ('member', 'admin')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_group_chat_memberships_role', 'group_chat_memberships', type_='check')
    op.drop_column('group_chat_memberships', 'role')
