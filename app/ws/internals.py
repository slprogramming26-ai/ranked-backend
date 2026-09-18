import secrets
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..config import settings
from .publisher import PROTOCOL_VERSION
from contextlib import contextmanager
from ..database import SessionLocal
from .. import schemas
from .manager import manager, ChatError, RekeyRequired, KeyOutdated
from . import presence, publisher



def verify_gateway(x_ws_secret: Optional[str] = Header(default=None)) -> None:
    """Türsteher für ALLE internen Endpoints.

    SICHERHEITSKRITISCH: unterhalb dieser Prüfung wird sender_id ungeprüft
    geglaubt. Wer hier durchkommt, kann als beliebiger Nutzer schreiben."""
    if not x_ws_secret or not secrets.compare_digest(x_ws_secret, settings.ws_internal_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid ws secret",
        )


router = APIRouter(
    prefix="/internal/ws",
    tags=["Internal WS"],
    dependencies=[Depends(verify_gateway)],
    # Nicht in /docs auflisten: diese Endpoints gehen nur den Gateway etwas an
    # und werden nach außen gar nicht erreichbar sein. Am Router, nicht an den
    # Funktionen -> gilt automatisch auch für einen künftigen dritten Endpoint.
    include_in_schema=False,
)


class _InternalBase(BaseModel):
    protocol_version: int
    sender_id: int          # von Go BEHAUPTET -> siehe verify_gateway
    to: int
    message: str = Field(min_length=1, max_length=4096)
    client_msg_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("protocol_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version {v} (server speaks {PROTOCOL_VERSION})")
        return v


class InternalDmIn(_InternalBase):
    """to = recipient user_id"""


class InternalGroupIn(_InternalBase):
    """to = group_chat_id"""
    key_version: int


@contextmanager
def _db_scope():
    """Absichtlich kopiert aus routes.py statt importiert: routes.py fällt in
    Phase 3/4 weg, internal.py bleibt. Sechs Zeilen Doppelung sind billiger
    als eine Abhängigkeit auf eine Datei, die verschwinden soll."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/dm")
def push_dm(incoming: InternalDmIn) -> dict:
    """Antwort = wörtlich das JSON, das Go dem Sender zurückreicht."""
    with _db_scope() as db:
        try:
            created_at, is_new = manager._prepare_dm_send(
                sender_id=incoming.sender_id,
                recipient_id=incoming.to,
                content=incoming.message,
                db=db,
                client_msg_id=incoming.client_msg_id,
            )
        except ChatError as e:
            return {"kind": "error", "detail": str(e)}


    # Duplikat (Reconnect-Race): steht schon in der DB und wurde ggf. schon
    # gepusht -> kein zweiter Push, nur das Ack.
    if not is_new:
        return schemas.ChatAck(to=incoming.to, delivered_live=0).model_dump(mode="json")

    out = schemas.ChatMessageOut(
        sender_id=incoming.sender_id,
        message=incoming.message,
        created_at=created_at,
        client_msg_id=incoming.client_msg_id,
    )
    publisher.push([incoming.to], out.model_dump(mode="json"))

    delivered_live = 1 if presence.is_online(incoming.to) else 0
    return schemas.ChatAck(to=incoming.to, delivered_live=delivered_live).model_dump(mode="json")



@router.post("/group")
def push_group(incoming: InternalGroupIn) -> dict:
    """Antwort = wörtlich das JSON, das Go dem Sender zurückreicht."""
    with _db_scope() as db:
        try:
            created_at, recipient_ids = manager._prepare_group_send(
                sender_id=incoming.sender_id,
                group_chat_id=incoming.to,
                content=incoming.message,
                key_version=incoming.key_version,
                db=db,
                client_msg_id=incoming.client_msg_id,
            )
        # Reihenfolge wichtig: die Spezialfaelle VOR dem generischen ChatError.
        except RekeyRequired:
            return {"kind": "rekey_required", "group_chat_id": incoming.to}
        except KeyOutdated as e:
            return {
                "kind": "key_outdated",
                "group_chat_id": incoming.to,
                "current_version": e.current_version,
            }
        except ChatError as e:
            return {"kind": "error", "detail": str(e)}

    out = schemas.GroupChatMessageOut(
        group_chat_id=incoming.to,
        sender_id=incoming.sender_id,
        message=incoming.message,
        created_at=created_at,
        key_version=incoming.key_version,
        client_msg_id=incoming.client_msg_id,
    )
    publisher.push(recipient_ids, out.model_dump(mode="json"))

    delivered_live = len(presence.online_user_ids(recipient_ids))
    return schemas.ChatAck(to=incoming.to, delivered_live=delivered_live).model_dump(mode="json")


