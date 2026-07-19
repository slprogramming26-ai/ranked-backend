"""add unique client_msg_id indexes

Revision ID: f7c2d84a1b93
Revises: e5b3a9c17f42
Create Date: 2026-07-19 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7c2d84a1b93'
down_revision: Union[str, Sequence[str], None] = 'e5b3a9c17f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Idempotenz fuer den Messenger serverseitig erzwingen: dieselbe
    # client_msg_id darf pro Absender nur EINMAL existieren. Alte Zeilen mit
    # client_msg_id = NULL kollidieren nicht (NULLs gelten als verschieden).
    #
    # Falls dieser upgrade mit "duplicate key" fehlschlaegt, liegen aus alten
    # Reconnect-Races schon echte Duplikate in der Tabelle — die muessen dann
    # einmalig von Hand bereinigt werden, bevor der Index angelegt werden kann.
    op.create_index(
        'uq_message_sender_client_msg_id',
        'message',
        ['sender_id', 'client_msg_id'],
        unique=True,
    )
    op.create_index(
        'uq_group_message_sender_client_msg_id',
        'group_message',
        ['sender_id', 'client_msg_id'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_group_message_sender_client_msg_id', table_name='group_message')
    op.drop_index('uq_message_sender_client_msg_id', table_name='message')
