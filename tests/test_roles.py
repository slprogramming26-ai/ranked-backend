"""Prueft die Sperr-Pruefung in get_current_user und require_role (oauth2.py).

Der Test braucht KEINE Datenbank: statt einer echten Session bekommt
get_current_user eine Attrappe, die bei query().filter().first() einfach den
vorbereiteten User zurueckgibt. Das Token ist echt (create_access_token),
damit verify_access_token wie im Betrieb laeuft.

Start:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_roles.py -v
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import models
from app.oauth2 import create_access_token, get_current_user, require_role


class FakeDB:
    """Spielt nur die eine Abfrage nach, die get_current_user macht."""

    def __init__(self, user):
        self.user = user

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return self.user


def make_user(role="user", banned_until=None):
    return models.User(id=1, username="test", role=role, banned_until=banned_until)


def call_current_user(user):
    token = create_access_token({"user_id": 1})
    return get_current_user(token=token, db=FakeDB(user))


# --- Sperre ---------------------------------------------------------------

def test_nicht_gesperrt_kommt_durch():
    user = make_user()
    assert call_current_user(user) is user


def test_aktive_sperre_gibt_403():
    user = make_user(banned_until=datetime.now(timezone.utc) + timedelta(days=1))
    with pytest.raises(HTTPException) as e:
        call_current_user(user)
    assert e.value.status_code == 403
    assert "suspended" in e.value.detail


def test_abgelaufene_sperre_hebt_sich_selbst_auf():
    user = make_user(banned_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert call_current_user(user) is user


def test_unbekannter_user_bleibt_401():
    with pytest.raises(HTTPException) as e:
        call_current_user(None)
    assert e.value.status_code == 401


# --- Rollen ---------------------------------------------------------------
# require_role(...) liefert den role_checker; den rufen wir direkt mit dem User
# auf, so wie FastAPI es nach get_current_user tun wuerde.

@pytest.mark.parametrize("role, min_role, erlaubt", [
    ("user", "user", True),
    ("user", "moderator", False),
    ("user", "admin", False),
    ("moderator", "moderator", True),
    ("moderator", "admin", False),
    ("admin", "moderator", True),   # Admin darf alles, was ein Moderator darf
    ("admin", "admin", True),
])
def test_require_role(role, min_role, erlaubt):
    user = make_user(role=role)
    checker = require_role(min_role)

    if erlaubt:
        assert checker(current_user=user) is user
    else:
        with pytest.raises(HTTPException) as e:
            checker(current_user=user)
        assert e.value.status_code == 403
