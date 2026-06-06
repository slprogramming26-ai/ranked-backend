"""swipe ranking rebuild: rename posts.category -> flag, rebuild ranking_scores

Revision ID: f3a9c1d4b2e7
Revises: d64161f27727
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d4b2e7'
down_revision: Union[str, Sequence[str], None] = 'd64161f27727'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # posts.category ist jetzt der Punkte-Multiplikator "flag" — nur umbenennen, Daten bleiben
    op.alter_column('posts', 'category', new_column_name='flag')

    # RankingScores: vom Paar-Rating-Modell auf das Swipe-Modell umgebaut.
    # Alte Datensaetze (target_user_id + 3 Ratings) sind inkompatibel und werden verworfen,
    # damit die neuen NOT-NULL-Spalten gesetzt werden koennen.
    op.execute('DELETE FROM ranking_scores')

    op.drop_column('ranking_scores', 'productivity_rating')
    op.drop_column('ranking_scores', 'engagement_rating')
    op.drop_column('ranking_scores', 'creativity_rating')
    op.drop_column('ranking_scores', 'target_user_id')

    op.add_column('ranking_scores', sa.Column('post_id', sa.Integer(), nullable=False))
    op.add_column('ranking_scores', sa.Column('direction', sa.Boolean(), nullable=False))
    op.add_column('ranking_scores', sa.Column('points', sa.Integer(), nullable=False))
    op.create_foreign_key(
        'ranking_scores_post_id_fkey', 'ranking_scores', 'posts',
        ['post_id'], ['id'], ondelete='CASCADE'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DELETE FROM ranking_scores')

    op.drop_constraint('ranking_scores_post_id_fkey', 'ranking_scores', type_='foreignkey')
    op.drop_column('ranking_scores', 'points')
    op.drop_column('ranking_scores', 'direction')
    op.drop_column('ranking_scores', 'post_id')

    op.add_column('ranking_scores', sa.Column('target_user_id', sa.Integer(), nullable=False))
    op.add_column('ranking_scores', sa.Column('creativity_rating', sa.Integer(), nullable=False))
    op.add_column('ranking_scores', sa.Column('engagement_rating', sa.Integer(), nullable=False))
    op.add_column('ranking_scores', sa.Column('productivity_rating', sa.Integer(), nullable=False))
    op.create_foreign_key(
        'ranking_scores_target_user_id_fkey', 'ranking_scores', 'users',
        ['target_user_id'], ['id'], ondelete='CASCADE'
    )

    op.alter_column('posts', 'flag', new_column_name='category')
