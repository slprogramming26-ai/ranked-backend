"""add username to users table

Revision ID: c98248c97a8e
Revises: 917505e8f7ad
Create Date: 2026-04-08 10:05:50.537974

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c98248c97a8e'
down_revision: Union[str, Sequence[str], None] = '917505e8f7ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hier fügen wir die Spalte mit der Unique-Constraint hinzu
    op.add_column("users", sa.Column("username", sa.String(), nullable=False, unique=True))

def downgrade() -> None:
    # Falls wir zurückrollen, entfernen wir die Spalte wieder
    op.drop_column("users", "username")