"""group_message one row per message (drop recipient_id)

Revision ID: b7d2e9f4a1c8
Revises: 237eefa188d5
Create Date: 2026-06-08 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e9f4a1c8'
down_revision: Union[str, Sequence[str], None] = '237eefa188d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Alte Zeilen waren pro-Empfänger-Duplikate aus dem alten pending/delete-Modell
    # (transiente, noch nicht zugestellte Nachrichten). Ohne Löschen würden sie nach
    # dem Drop zu mehrfach identischen Zeilen pro Nachricht → Client sähe Duplikate.
    op.execute('DELETE FROM group_message')
    op.drop_column('group_message', 'recipient_id')


def downgrade() -> None:
    """Downgrade schema."""
    # nullable=True, weil neue Zeilen (nach dem Upgrade) kein recipient_id mehr haben
    # und ein NOT NULL beim Re-Add sonst auf einer nicht-leeren Tabelle scheitern würde.
    op.add_column(
        'group_message',
        sa.Column('recipient_id', sa.INTEGER(), nullable=True),
    )
