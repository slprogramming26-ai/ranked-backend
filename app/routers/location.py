from fastapi import APIRouter, Depends, Header, status, HTTPException
from typing import List, Optional
from .. import schemas, database, models, oauth2
from sqlalchemy.orm import Session
from ..config import settings

router = APIRouter(
    prefix="/locations",
    tags=["Locations"]
)


@router.get("/", response_model=List[schemas.LocationOut])
def get_locations(search: Optional[str] = "", db: Session = Depends(database.get_dp), current_user: int = Depends(oauth2.get_current_user)):

    pattern = f"%{search}%"
    prefix = f"{search}%"

    locations = (
        db.query(models.Location)
        .filter(models.Location.name.ilike(pattern))
        .order_by(
            models.Location.name.ilike(prefix).desc(),
            models.Location.name.asc(),
        )
        .limit(20)
        .all()
    )

    return locations



@router.post("/", status_code=status.HTTP_201_CREATED)
def create_locations(payload: schemas.LocationBulkCreate, db: Session = Depends(database.get_dp), x_location_admin_secret: Optional[str] = Header(default=None)):
    """Bulk-Upload fuer den Orts-Katalog. Geschuetzt durch Header-Secret, kein Login-Token (wie Story-Cleanup)."""
    if x_location_admin_secret != settings.location_admin_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid location admin secret")

    created = 0
    skipped = 0

    for name in payload.names:
        name = name.strip()

        if not name:
            continue

        existing = db.query(models.Location).filter(models.Location.name == name).first()
        if existing:
            skipped += 1
            continue

        db.add(models.Location(name=name))
        created += 1

    db.commit()

    return {"created": created, "skipped": skipped}

