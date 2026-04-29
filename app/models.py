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
    favorite_animal = Column(String, nullable= True)
    favorite_snack = Column(String, nullable= True)
    ideal_weekend = Column(String, nullable= True)
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


class RankingScores(Base):

    __tablename__ = 'ranking_scores'

    id = Column(Integer, primary_key=True, nullable=False)
    voter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    
    # Deine Kriterien
    productivity_rating = Column(Integer, nullable=False) # 1-10
    engagement_rating = Column(Integer, nullable=False)   # 1-10
    creativity_rating = Column(Integer, nullable=False)
    
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    