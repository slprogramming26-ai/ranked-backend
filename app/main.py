from fastapi import FastAPI
from . import models
from .database import engine
from .routers import group_chat, post, user, auth, vote, comment,ranking, follow, message, key, story
from .ws import routes as ws_routes
from fastapi.middleware.cors import CORSMiddleware



app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Für Entwicklung okay, später einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(post.router)
app.include_router(user.router)
app.include_router(auth.router)
app.include_router(vote.router)
app.include_router(comment.router)
app.include_router(ranking.router)
app.include_router(follow.router)
app.include_router(ws_routes.router)
app.include_router(group_chat.router)
app.include_router(message.router)
app.include_router(key.router)
app.include_router(story.router)

# models.Base.metadata.create_all(bind=engine)



@app.get("/")
def root():
    return {"message": "Hello World"}