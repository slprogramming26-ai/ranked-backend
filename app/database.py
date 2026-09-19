from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = f'postgresql://{settings.database_username}:{settings.database_passwort}@{settings.database_hostname}:{settings.database_port}/{settings.database_name}?sslmode=require'

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"sslmode": "require"},
    pool_pre_ping=True,
    # Supavisor im Transaktions-Modus (Port 6543): 15 echte Postgres-Leitungen
    # (Pool Size, Nano) fuer ALLE Worker zusammen, bis zu 200 Client-Verbindungen.
    # Pro Worker hoechstens 15 -> kein Worker stellt mehr Anfragen an, als es
    # Leitungen gibt. 4 Worker = 60, plus lokal 15 = 75 von 200.
    pool_size=5,         # dauerhaft offen gehaltene Verbindungen
    max_overflow=10,     # zusaetzliche bei Lastspitzen (werden danach geschlossen)
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
        