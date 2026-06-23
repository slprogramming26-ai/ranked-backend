from .database import Base
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.sql.sqltypes import TIMESTAMP, DATE
from sqlalchemy.sql.expression import null, text
from sqlalchemy.orm import relationship
from .database import Base

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

    owner = relationship("User")


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


class GroupChatMembership(Base):

    __tablename__ = 'group_chat_memberships'

    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False, primary_key=True)
    participant_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    joined_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class GroupMessage(Base):

    __tablename__ = 'group_message'

    # Eine Zeile pro Nachricht (Option A) — KEIN recipient_id mehr.
    # Wer die Nachricht sehen darf, ergibt sich aus der Mitgliedschaft +
    # joined_at (Mitglieder sehen nur, was nach ihrem Beitritt gesendet wurde).
    id = Column(Integer, primary_key=True, nullable=False)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    # Vom Client erzeugte UUID pro Nachricht (Idempotenz-/Dedup-Key).
    # nullable=True, weil Altbestand keinen Key hat.
    client_msg_id = Column(String, nullable=True)



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
    reason = Column(String, nullable=False) # z.B. "Spam", "Beleidigung", "Unangebracht"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class UserKey(Base):
    __tablename__ = 'user_keys'

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    public_key = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class RefreshToken(Base):
    __tablename__ = 'refresh_tokens'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Wir speichern NICHT den Token selbst, sondern seinen Hash (SHA-256).
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
