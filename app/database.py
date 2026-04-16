from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .config import settings
import psycopg2


SQLALCHEMY_DATABASE_URL = f'postgresql://{settings.database_username}:{settings.database_passwort}@{settings.database_hostname}:{settings.database_port}/{settings.database_name}'

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_dp():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()