"""reports: target_type + target_id statt post_id/story_id/comment_id

Meldungen sollen das Loeschen des Inhalts ueberleben. Die drei CASCADE-Fremdschluessel
werden durch target_type + target_id (ohne FK) ersetzt, dazu eine Text-Kopie.

Revision ID: a491945c9e59
Revises: 4b4b0f83a716
Create Date: 2026-10-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a491945c9e59'
down_revision: Union[str, Sequence[str], None] = '4b4b0f83a716'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Neue Spalten. target_type erst nullable, weil die alten Zeilen noch leer sind.
    op.add_column('reports', sa.Column('target_type', sa.String(), nullable=True))
    op.add_column('reports', sa.Column('target_id', sa.Integer(), nullable=True))
    op.add_column('reports', sa.Column('content_snapshot', sa.String(), nullable=True))

    # 2. Bestehende Meldungen umrechnen: welche der drei Spalten gesetzt ist, bestimmt den Typ.
    op.execute("""
        UPDATE reports SET
            target_type = CASE
                WHEN post_id    IS NOT NULL THEN 'post'
                WHEN story_id   IS NOT NULL THEN 'story'
                WHEN comment_id IS NOT NULL THEN 'comment'
                ELSE 'user'
            END,
            target_id = COALESCE(post_id, story_id, comment_id)
    """)

    # 3. Text-Kopie fuer bestehende Meldungen nachholen (Inhalte existieren ja noch).
    op.execute("""
        UPDATE reports r SET content_snapshot = p.title || E'\\n\\n' || p.content
        FROM posts p WHERE r.target_type = 'post' AND r.target_id = p.id
    """)
    op.execute("""
        UPDATE reports r SET content_snapshot = c.comment
        FROM comments c WHERE r.target_type = 'comment' AND r.target_id = c.id
    """)

    # 4. Jetzt sind alle Zeilen gefuellt -> Pflichtfeld + Regeln + Index.
    op.alter_column('reports', 'target_type', nullable=False)
    op.create_check_constraint('ck_reports_target_type', 'reports',
                               "target_type IN ('post', 'story', 'comment', 'user')")
    op.create_check_constraint('ck_reports_target_id', 'reports',
                               "(target_type = 'user') = (target_id IS NULL)")
    op.create_index('ix_reports_target_type_target_id', 'reports', ['target_type', 'target_id'])

    # 5. Alte Spalten samt CASCADE-Fremdschluesseln weg.
    op.drop_constraint('reports_post_id_fkey', 'reports', type_='foreignkey')
    op.drop_constraint('reports_story_id_fkey', 'reports', type_='foreignkey')
    op.drop_constraint('reports_comment_id_fkey', 'reports', type_='foreignkey')
    op.drop_column('reports', 'post_id')
    op.drop_column('reports', 'story_id')
    op.drop_column('reports', 'comment_id')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('reports', sa.Column('post_id', sa.Integer(), nullable=True))
    op.add_column('reports', sa.Column('story_id', sa.Integer(), nullable=True))
    op.add_column('reports', sa.Column('comment_id', sa.Integer(), nullable=True))

    # Meldungen zu inzwischen geloeschten Inhalten passen nicht mehr unter einen FK -> weg.
    op.execute("""
        DELETE FROM reports r WHERE
            (r.target_type = 'post'    AND NOT EXISTS (SELECT 1 FROM posts p    WHERE p.id = r.target_id)) OR
            (r.target_type = 'story'   AND NOT EXISTS (SELECT 1 FROM stories s  WHERE s.id = r.target_id)) OR
            (r.target_type = 'comment' AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.id = r.target_id))
    """)
    op.execute("UPDATE reports SET post_id = target_id    WHERE target_type = 'post'")
    op.execute("UPDATE reports SET story_id = target_id   WHERE target_type = 'story'")
    op.execute("UPDATE reports SET comment_id = target_id WHERE target_type = 'comment'")

    op.create_foreign_key('reports_post_id_fkey', 'reports', 'posts', ['post_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('reports_story_id_fkey', 'reports', 'stories', ['story_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('reports_comment_id_fkey', 'reports', 'comments', ['comment_id'], ['id'], ondelete='CASCADE')

    op.drop_index('ix_reports_target_type_target_id', table_name='reports')
    op.drop_constraint('ck_reports_target_id', 'reports', type_='check')
    op.drop_constraint('ck_reports_target_type', 'reports', type_='check')
    op.drop_column('reports', 'content_snapshot')
    op.drop_column('reports', 'target_id')
    op.drop_column('reports', 'target_type')
