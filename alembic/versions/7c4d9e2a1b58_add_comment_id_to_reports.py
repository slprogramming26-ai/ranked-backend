"""add comment_id to reports

Revision ID: 7c4d9e2a1b58
Revises: 38ef72cbf703
Create Date: 2026-07-03 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c4d9e2a1b58'
down_revision: Union[str, Sequence[str], None] = '38ef72cbf703'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('reports', sa.Column('comment_id', sa.Integer(), nullable=True))
    op.create_foreign_key('reports_comment_id_fkey', 'reports', 'comments', ['comment_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('reports_comment_id_fkey', 'reports', type_='foreignkey')
    op.drop_column('reports', 'comment_id')
