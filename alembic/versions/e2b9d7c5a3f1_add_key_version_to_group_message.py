"""add group_chat_epochs, key_version on group_message, link via FKs

Revision ID: e2b9d7c5a3f1
Revises: c4e1f0a7b2d9
Create Date: 2026-06-27 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b9d7c5a3f1'
down_revision: Union[str, Sequence[str], None] = 'c4e1f0a7b2d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Eine Zeile pro Schlüssel-Epoche einer Gruppe. (group_chat_id, key_version) ist
    # Primary Key -> eindeutiges Ziel für die ForeignKeys aus keys und messages.
    op.create_table('group_chat_epochs',
    sa.Column('group_chat_id', sa.Integer(), nullable=False),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['group_chat_id'], ['group_chats.group_chat_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('group_chat_id', 'key_version')
    )

    # Sicherheit: falls group_chat_keys schon Zeilen hat, deren Epochen in die neue
    # Tabelle übernehmen, sonst würde der folgende FK fehlschlagen. (Bei leerer
    # Tabelle ein No-Op.)
    op.execute(
        "INSERT INTO group_chat_epochs (group_chat_id, key_version) "
        "SELECT DISTINCT group_chat_id, key_version FROM group_chat_keys"
    )

    # group_chat_keys.(group_chat_id, key_version) -> group_chat_epochs
    op.create_foreign_key(
        'fk_group_chat_keys_epoch',
        'group_chat_keys', 'group_chat_epochs',
        ['group_chat_id', 'key_version'],
        ['group_chat_id', 'key_version'],
        ondelete='CASCADE',
    )

    # Mit welcher Epoche eine Gruppen-Nachricht verschlüsselt wurde (NULL = Altbestand).
    op.add_column('group_message', sa.Column('key_version', sa.Integer(), nullable=True))

    # group_message.(group_chat_id, key_version) -> group_chat_epochs
    op.create_foreign_key(
        'fk_group_message_epoch',
        'group_message', 'group_chat_epochs',
        ['group_chat_id', 'key_version'],
        ['group_chat_id', 'key_version'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_group_message_epoch', 'group_message', type_='foreignkey')
    op.drop_column('group_message', 'key_version')
    op.drop_constraint('fk_group_chat_keys_epoch', 'group_chat_keys', type_='foreignkey')
    op.drop_table('group_chat_epochs')
