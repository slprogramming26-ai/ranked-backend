from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from typing import Optional, Literal
from pydantic.types import conint

class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    created_at: datetime
    ranking_enabled: bool
    profile_picture_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class PostBase(BaseModel):
    title: str
    content: str
    published: bool = True
    


class PostCreate(PostBase):
    image_url: Optional[str] = None
    category: Optional[str] = None
    pass 

class Post(PostBase):
    id: int
    created_at: datetime
    owner_id: int
    owner: UserOut
    model_config = ConfigDict(from_attributes=True)
    image_url: Optional[str] = None
    category: Optional[str] = None

class PostOut(BaseModel):
    post: Post   
    votes: int
    model_config = ConfigDict(from_attributes=True)








class UserCreate(BaseModel):
    email: EmailStr
    username: str
    passwort: str
    

class UserDetails(BaseModel):
    vibe_factor_1: Optional[str] = None
    vibe_factor_2: Optional[str] = None
    profile_picture_url: Optional[str] = None 
    biography: Optional[str] = None
    ranking_enabled: Optional[bool] = None

class GetUserOut(BaseModel):
    id: int 
    email: Optional[EmailStr] = None
    username: str
    vibe_factor_1: Optional[str] = None 
    vibe_factor_2: Optional[str] = None
    profile_picture_url: Optional[str] = None 
    biography: Optional[str] = None
    ranking_enabled: bool
    follower_count: Optional[int] = None
    is_followed: Optional[bool] = None
 
    model_config = ConfigDict(from_attributes=True)


class UserLogin(BaseModel):
    email: EmailStr
    passwort: str







class Token(BaseModel):
    access_token : str
    toke_type : str
    

class TokenData(BaseModel):
    id: int






class Vote(BaseModel):
    post_id: int
    dir: int = Field(le=1)


class Follow(BaseModel):
    followee_id: int
    dir: int = Field(le=1)
    









class CommentBase(BaseModel):
    post_id: int
    comment: str

    
class CreateComment(CommentBase):
    pass

class CommentOut(BaseModel):
    username: str
    post_id: int
    comment: str

    model_config = ConfigDict(from_attributes=True)



class RankingScore(BaseModel):
    id: int
    voter_id: int
    target_user_id: int
    
    
    productivity_rating: int = Field(ge=1, le=100) 
    engagement_rating: int = Field(ge=1, le=100)   
    creativity_rating: int = Field(ge=1, le=100)
    
    created_at : datetime

class RankingScoreOut(BaseModel):
    voter_id: int
    target_user_id: int
    productivity_rating: int
    engagement_rating: int
    creativity_rating: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class RankingScoreCreate(BaseModel):
    target_user_id: int
    
    
    productivity_rating: int = Field(ge=1, le=100) 
    engagement_rating: int = Field(ge=1, le=100)   
    creativity_rating: int = Field(ge=1, le=100)


class LeaderboardEntry(BaseModel):
    target_user_id: int
    username: str
    profile_picture_url: Optional[str] = None
    avg_productivity: float
    avg_creativity: float
    avg_engagement: float
    total_ratings: int
    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Chat / WebSocket Schemas
# =========================================================
# Alle Chat-Nachrichten laufen über EINEN WebSocket-Endpoint.
# Damit Server + Client wissen worum's bei jeder einzelnen Nachricht geht,
# trägt jede Nachricht ein "kind"-Feld:
#   "dm"      → Direct Message  (1:1)
#   "group"   → Gruppen-Nachricht
#   "ack"     → Bestätigung vom Server an den Sender ("ist raus")
#   "error"   → wenn was schief lief (z.B. Validierung)
#
# Literal["dm"] sorgt dafür dass Pydantic die Nachricht nur akzeptiert
# wenn EXAKT "dm" drinsteht — wer "DM" oder "direct" schickt wird abgewiesen.


class ChatMessageIn(BaseModel):
    """DM vom Client. JSON: { "kind": "dm", "to": <user_id>, "message": "..." }"""
    kind: Literal["dm"]
    to: int  # recipient user_id
    message: str = Field(min_length=1, max_length=2000)

    # extra="forbid" → unbekannte Felder werden abgewiesen statt ignoriert
    # (kleine zusätzliche Härtung gegen versehentliche oder bösartige Payloads)
    model_config = ConfigDict(extra="forbid")


class GroupChatMessageIn(BaseModel):
    """Group-Message vom Client. JSON: { "kind": "group", "to": <group_chat_id>, "message": "..." }"""
    kind: Literal["group"]
    to: int  # group_chat_id
    message: str = Field(min_length=1, max_length=2000)

    model_config = ConfigDict(extra="forbid")


class ChatMessageOut(BaseModel):
    """Eingehende DM die der Empfänger über seinen Socket bekommt."""
    kind: Literal["dm"] = "dm"
    sender_id: int
    message: str
    created_at: datetime


class GroupChatMessageOut(BaseModel):
    """Eingehende Gruppen-Nachricht die ein Gruppen-Mitglied bekommt."""
    kind: Literal["group"] = "group"
    group_chat_id: int
    sender_id: int
    message: str
    created_at: datetime


class ChatAck(BaseModel):
    """Bestätigung an den Sender nach jedem Send.
    delivered_live = Anzahl Empfänger die's live bekommen haben.
      - DM:     0 (Empfänger offline, gequeued) oder 1 (live ausgeliefert)
      - Group:  0..n (Anzahl online Mitglieder die's bekommen haben)
    Der Rest landet in der pending Queue und wird beim Reconnect ausgeliefert."""
    kind: Literal["ack"] = "ack"
    to: int  # user_id (bei DM) oder group_chat_id (bei group)
    delivered_live: int


class GroupChatUpdate(BaseModel):
    group_name: Optional[str] = None
    profile_picture: Optional[str] = None


class GroupChatInformationOut(BaseModel):
    group_chat_id: int
    group_name: Optional[str] = None
    profile_picture: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)