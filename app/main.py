from fastapi import FastAPI
from . import models
from .database import engine
from .routers import group_chat, post, user, auth, vote, comment,location, cleanup, ranking, follow, message, key, story, report, impression
from .ws import routes as ws_routes
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .limiter import limiter          # <- der EINE Limiter aus limiter.py







app = FastAPI()

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)




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
app.include_router(report.router)
app.include_router(location.router)
app.include_router(cleanup.router)
app.include_router(impression.router)


# models.Base.metadata.create_all(bind=engine)



@app.get("/")
def root():
    return {"message": "Hello World"}