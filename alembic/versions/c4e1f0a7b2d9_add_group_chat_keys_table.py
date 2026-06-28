"""add group_chat_keys table

Revision ID: c4e1f0a7b2d9
Revises: a1a03ad5c56d
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision: str = 'c4e1f0a7b2d9'
down_revision: Union[str, Sequence[str], None] = 'a1a03ad5c56d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('group_chat_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('group_chat_id', sa.Integer(), nullable=False),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.Column('recipient_id', sa.Integer(), nullable=False),
    sa.Column('encrypted_key', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['group_chat_id'], ['group_chats.group_chat_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipient_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_chat_id', 'key_version', 'recipient_id', name='uq_group_key_version_recipient')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('group_chat_keys')
