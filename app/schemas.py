from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from typing import Optional
from pydantic.types import conint

class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class PostBase(BaseModel):
    title: str
    content: str
    published: bool = True
    


class PostCreate(PostBase):
    image_url: Optional[str] = None
    pass 

class Post(PostBase):
    id: int
    created_at: datetime
    owner_id: int
    owner: UserOut
    model_config = ConfigDict(from_attributes=True)
    image_url: Optional[str] = None

class PostOut(BaseModel):
    post: Post   
    votes: int
    model_config = ConfigDict(from_attributes=True)








class UserCreate(BaseModel):
    email: EmailStr
    username: str
    passwort: str
    

class UserDetails(BaseModel):
    vibe_factor_1: str
    vibe_factor_2: str
    profile_picture_url: Optional[str] = None 
    biography: str

class GetUserOut(BaseModel):
    email: EmailStr
    username: str
    vibe_factor_1: Optional[str] = None 
    vibe_factor_2: Optional[str] = None
    profile_picture_url: Optional[str] = None 
    biography: str

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
    
    
    productivity_rating: int = Field(ge=1, le=10) 
    engagement_rating: int = Field(ge=1, le=10)   
    creativity_rating: int = Field(ge=1, le=10)
    
    created_at : datetime

class RankingScoreOut(BaseModel):
    voter_id: int
    target_user_id: int
    productivity_rating: int
    engagement_rating: int
    creativity_rating: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
    
