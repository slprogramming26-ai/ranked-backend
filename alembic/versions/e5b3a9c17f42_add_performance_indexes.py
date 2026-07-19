"""add performance indexes

Revision ID: e5b3a9c17f42
Revises: 4bc26d4173ab
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b3a9c17f42'
down_revision: Union[str, Sequence[str], None] = '4bc26d4173ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # posts: Feed (Score + Sortierung), Profil, Lokal-Feed
    op.create_index('ix_posts_created_at', 'posts', ['created_at'])
    op.create_index('ix_posts_owner_id', 'posts', ['owner_id'])
    op.create_index('ix_posts_location_id', 'posts', ['location_id'])

    # votes: Feed-Join zaehlt Votes pro Post (PK deckt nur user_id ab)
    op.create_index('ix_votes_post_id', 'votes', ['post_id'])

    # comments: Kommentare eines Posts laden
    op.create_index('ix_comments_post_id', 'comments', ['post_id'])

    # follows: Follower-Count + Story-Feed (PK deckt nur follower_id ab)
    op.create_index('ix_follows_followee_id', 'follows', ['followee_id'])

    # daily_targets: Tages-Match des Bewerters finden
    op.create_index('ix_daily_targets_voter_id', 'daily_targets', ['voter_id'])

    # ranking_scores: Tages-Sperre (voter), Leaderboard (created_at + Join auf post)
    op.create_index('ix_ranking_scores_voter_id', 'ranking_scores', ['voter_id'])
    op.create_index('ix_ranking_scores_post_id', 'ranking_scores', ['post_id'])
    op.create_index('ix_ranking_scores_created_at', 'ranking_scores', ['created_at'])

    # message: Chat-Sync — nach Person filtern, nach Zeit eingrenzen/sortieren
    op.create_index('ix_message_recipient_id_created_at', 'message', ['recipient_id', 'created_at'])
    op.create_index('ix_message_sender_id_created_at', 'message', ['sender_id', 'created_at'])

    # group_message: Gruppen-Sync — nach Gruppe filtern, nach Zeit eingrenzen/sortieren
    op.create_index('ix_group_message_group_chat_id_created_at', 'group_message', ['group_chat_id', 'created_at'])

    # group_chat_memberships: "meine Gruppen" sucht nach participant_id (zweite PK-Spalte)
    op.create_index('ix_group_chat_memberships_participant_id', 'group_chat_memberships', ['participant_id'])

    # activities: Activity-Liste des Users, neueste zuerst
    op.create_index('ix_activities_user_id_created_at', 'activities', ['user_id', 'created_at'])

    # blocks: Block-Check laeuft in beide Richtungen (PK deckt nur blocker_id ab)
    op.create_index('ix_blocks_blocked_id', 'blocks', ['blocked_id'])

    # reports: "was habe ICH gemeldet?" bei jedem Feed-/Story-/Comment-Abruf
    op.create_index('ix_reports_reporter_id', 'reports', ['reporter_id'])

    # refresh_tokens: alle Tokens eines Users (Logout ueberall / CASCADE beim Loeschen)
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'])

    # stories: Story-Feed (owner + Alter) und Cleanup (Alter)
    op.create_index('ix_stories_owner_id', 'stories', ['owner_id'])
    op.create_index('ix_stories_created_at', 'stories', ['created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_stories_created_at', table_name='stories')
    op.drop_index('ix_stories_owner_id', table_name='stories')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_index('ix_reports_reporter_id', table_name='reports')
    op.drop_index('ix_blocks_blocked_id', table_name='blocks')
    op.drop_index('ix_activities_user_id_created_at', table_name='activities')
    op.drop_index('ix_group_chat_memberships_participant_id', table_name='group_chat_memberships')
    op.drop_index('ix_group_message_group_chat_id_created_at', table_name='group_message')
    op.drop_index('ix_message_sender_id_created_at', table_name='message')
    op.drop_index('ix_message_recipient_id_created_at', table_name='message')
    op.drop_index('ix_ranking_scores_created_at', table_name='ranking_scores')
    op.drop_index('ix_ranking_scores_post_id', table_name='ranking_scores')
    op.drop_index('ix_ranking_scores_voter_id', table_name='ranking_scores')
    op.drop_index('ix_daily_targets_voter_id', table_name='daily_targets')
    op.drop_index('ix_follows_followee_id', table_name='follows')
    op.drop_index('ix_comments_post_id', table_name='comments')
    op.drop_index('ix_votes_post_id', table_name='votes')
    op.drop_index('ix_posts_location_id', table_name='posts')
    op.drop_index('ix_posts_owner_id', table_name='posts')
    op.drop_index('ix_posts_created_at', table_name='posts')


