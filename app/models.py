from .database import Base
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, UniqueConstraint, ForeignKeyConstraint
from sqlalchemy.sql.sqltypes import TIMESTAMP, DATE
from sqlalchemy.sql.expression import null, text
from sqlalchemy.orm import relationship
from .database import Base


class Location(Base):
    __tablename__ = 'locations'

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String, nullable=False, unique=True)



class Post(Base):
    __tablename__ = 'posts'

    id = Column(Integer, primary_key= True, nullable= False)
    title = Column(String, nullable= False)
    content = Column(String, nullable= False)
    published = Column(Boolean, server_default = 'True')
    created_at = Column(TIMESTAMP(timezone=True), nullable= False, server_default= text('now()'))
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable = False )
    image_url = Column(String, nullable=True)
    flag = Column(String, nullable= True)  # "engagement" | "creativity" | "productivity" | None — Punkte-Multiplikator
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)


    owner = relationship("User")
    location = relationship("Location")


class User(Base):

    __tablename__ = 'users'

    id = Column(Integer, primary_key= True, nullable= False)
    email = Column(String, nullable=False, unique= True)
    passwort = Column(String, nullable= False)
    username = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable= False, server_default= text('now()'))
    vibe_factor_1 = Column(String, nullable= True)
    vibe_factor_2 = Column(String, nullable= True)
    biography = Column(String, nullable= True)
    profile_picture_url = Column(String, nullable= True)
    ranking_enabled = Column(Boolean, server_default='False', nullable=False)
    xp = Column(Integer, nullable=False, server_default=text('0'))
    streak_count = Column(Integer, nullable=False, server_default=text('0'))
    last_swipe_date = Column(DATE, nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    location = relationship("Location")






class Votes(Base):

    __tablename__ = 'votes'
    
    user_id = Column(Integer,ForeignKey("users.id", ondelete="CASCADE"), primary_key= True,)
    post_id = Column(Integer,ForeignKey("posts.id", ondelete="CASCADE"), primary_key= True)


class Comments(Base):

    __tablename__ = 'comments'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer,ForeignKey("users.id", ondelete="CASCADE"),)
    post_id = Column(Integer,ForeignKey("posts.id", ondelete="CASCADE"),)
    comment = Column(String, nullable=False)

class DailyTarget(Base):
    __tablename__ = 'daily_targets'

    id = Column(Integer, primary_key=True, nullable=False)
    voter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False) # Wer schaut?
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False) # Wer wird bewertet?
    date = Column(DATE, nullable=False, server_default=text('now()'))
    
    voter = relationship("User", foreign_keys=[voter_id])
    target_user = relationship("User", foreign_keys=[target_user_id])


class RankingScores(Base):

    __tablename__ = 'ranking_scores'

    id = Column(Integer, primary_key=True, nullable=False)
    voter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)

    direction = Column(Boolean, nullable=False)  # True = Rechts/Cool, False = Links/Nicht cool
    points = Column(Integer, nullable=False)      # vom Server berechnet aus post.flag + direction

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    post = relationship("Post")



class DailyBonusLog(Base):
    __tablename__ = 'daily_bonus_log'

    date = Column(DATE, primary_key=True, nullable=False)
    processed_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    awarded_count = Column(Integer, nullable=False, server_default=text('0'))


class Activity(Base):
    __tablename__ = 'activities'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)  # Empfänger
    type = Column(String, nullable=False)  # "rated" (später auch "placement"/"streak"/"badge")
    payload = Column(Integer, nullable=False)  # z.B. erhaltene Punkte bei "rated"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))



class Follows(Base):

    __tablename__ = 'follows'

    follower_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,  primary_key= True)
    followee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,  primary_key= True)





class Message(Base):
   

    __tablename__ = 'message'

    id = Column(Integer, primary_key=True, nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    # Vom Client erzeugte UUID pro Nachricht (Idempotenz-/Dedup-Key).
    # nullable=True, weil Altbestand keinen Key hat.
    client_msg_id = Column(String, nullable=True)


class GroupChats(Base):
    

    __tablename__ = 'group_chats'

    group_chat_id = Column(Integer, primary_key=True, nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    group_name = Column(String, nullable=True)
    profile_picture = Column(String, nullable=True)

    # "dirty"-Flag für Lazy Rekeying: Wird bei jeder Mitglieder-Änderung (Beitritt/
    # Verlassen/Kick) auf True gesetzt. Der nächste Sender erzeugt dann eine neue
    # Schlüssel-Epoche, verteilt sie und setzt das Flag zurück auf False.
    needs_rekey = Column(Boolean, nullable=False, server_default='False')

class GroupChatJoinCodes(Base):

    __tablename__ = 'group_chats_join_codes'

    id = Column(Integer, primary_key=True, nullable=False)
    code = Column(Integer, nullable=False, unique=True)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))



class GroupChatMembership(Base):

    __tablename__ = 'group_chat_memberships'

    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False, primary_key=True)
    participant_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    joined_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class GroupMessage(Base):

    __tablename__ = 'group_message'

    id = Column(Integer, primary_key=True, nullable=False)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    client_msg_id = Column(String, nullable=True)
    # Mit welcher Schlüssel-Epoche der Gruppe diese Nachricht verschlüsselt wurde.
    key_version = Column(Integer, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ['group_chat_id', 'key_version'],
            ['group_chat_epochs.group_chat_id', 'group_chat_epochs.key_version'],
            ondelete='CASCADE',
        ),
    )




class GroupChatEpoch(Base):
    __tablename__ = 'group_chat_epochs'
    #epoche ist das akutelle schloss für gruppe group_chat id mit bestimmte schlüssel version
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), primary_key=True, nullable=False)
    key_version = Column(Integer, primary_key=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Block(Base):
    __tablename__ = 'blocks'
    blocker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    blocked_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Report(Base):
    __tablename__ = 'reports'
    id = Column(Integer, primary_key=True, nullable=False)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reported_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Was wurde gemeldet? Genau eines gesetzt = Post/Story/Comment-Report, alle NULL = User selbst (Name/Profilbild)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=True)
    story_id = Column(Integer, ForeignKey("stories.id", ondelete="CASCADE"), nullable=True)
    comment_id = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)
    reason = Column(String, nullable=False) # z.B. "Spam", "Beleidigung", "Unangebracht"
    status = Column(String, nullable=False, server_default=text("'pending'")) # pending / dismissed / action_taken
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class UserKey(Base):
    __tablename__ = 'user_keys'

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    public_key = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class GroupChatKey(Base):
    __tablename__ = 'group_chat_keys'

    # Eine Zeile = die verschlüsselte Kopie des Gruppenschlüssels (Version key_version)
    # der Gruppe group_chat_id, bestimmt für das Mitglied recipient_id.
    id = Column(Integer, primary_key=True, nullable=False)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    # Epoche des Gruppenschlüssels. Steigt bei jedem Rekey (Beitritt/Kick/Verlassen) um 1.
    # Eine neue Version erhält nur, wer zu diesem Zeitpunkt Mitglied ist -> keine History.
    key_version = Column(Integer, nullable=False)
    # Für welches Mitglied diese Kopie bestimmt ist.
    recipient_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Der symmetrische Gruppenschlüssel, verschlüsselt mit dem Public Key des recipient.
    # Der Server sieht den Klartext-Key nie.
    encrypted_key = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    # Pro Mitglied genau eine Kopie je Version.
    # Zusätzlich: zusammengesetzter FK auf die Epochen-Tabelle, damit keine Schlüssel-
    # Kopie auf eine nicht existierende Epoche zeigen kann.
    __table_args__ = (
        UniqueConstraint('group_chat_id', 'key_version', 'recipient_id', name='uq_group_key_version_recipient'),
        ForeignKeyConstraint(
            ['group_chat_id', 'key_version'],
            ['group_chat_epochs.group_chat_id', 'group_chat_epochs.key_version'],
            ondelete='CASCADE',
        ),
    )


class RefreshToken(Base):
    __tablename__ = 'refresh_tokens'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Wir speichern NICHT den Token selbst, sondern seinen Hash (SHA-256).
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Story(Base):
    __tablename__ = 'stories'

    id = Column(Integer, primary_key=True, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    image_url = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    owner = relationship("User")
