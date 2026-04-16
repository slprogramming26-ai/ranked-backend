from fastapi import FastAPI
from . import models
from .database import engine
from .routers import post, user, auth, vote, comment
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

# models.Base.metadata.create_all(bind=engine)



@app.get("/")
def root():
    return {"message": "Hello World"}