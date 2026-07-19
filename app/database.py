from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = f'postgresql://{settings.database_username}:{settings.database_passwort}@{settings.database_hostname}:{settings.database_port}/{settings.database_name}?sslmode=require'

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"sslmode": "require"},
    pool_pre_ping=True,
    # FastAPI bedient sync-Endpoints mit bis zu 40 Threads parallel — die
    # Default-Poolgroesse (5 + 10 Overflow) wird da unter Last zum Engpass.
    pool_size=10,        # dauerhaft offen gehaltene Verbindungen
    max_overflow=20,     # zusaetzliche Verbindungen bei Lastspitzen (werden danach geschlossen)
    pool_recycle=1800,   # Verbindungen nach 30 min erneuern, bevor Supabase sie serverseitig kappt
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_dp():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()       