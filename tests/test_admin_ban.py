"""Prueft die Sperr-Regeln in admin.py (ban_user / unban_user) und die
Sperr-Pruefung beim Login.

Wie test_roles.py OHNE Datenbank: die Routen-Funktionen werden direkt
aufgerufen, mit einer Attrappe als Session. So bleiben die echten
(Test-)Accounts in Supabase unberuehrt.

Start:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_admin_ban.py -v
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import models, schemas
from app.routers.admin import ban_user, unban_user, MOD_MAX_BAN_DAYS, PERMANENT_BAN_UNTIL
from app.routers.auth import login


class FakeQuery:
    def __init__(self, db, model):
        self.db, self.model = db, model

    def filter(self, *args):
        return self

    def first(self):
        return self.db.user if self.model is models.User else None

    def delete(self):
        if self.model is models.RefreshToken:
            self.db.tokens_deleted = True


class FakeDB:
    """Kennt genau EINEN User (das Ziel) und merkt sich, was passiert ist."""

    def __init__(self, user):
        self.user = user
        self.tokens_deleted = False
        self.committed = False
        self.added = []

    def query(self, model):
        return FakeQuery(self, model)

    def get(self, model, id):
        # _get_target_below_me laedt per db.get (Session-Cache statt neuer Abfrage)
        return self.user if model is models.User else None

    def add(self, obj):
        self.added.append(obj)

    def logged(self):
        """Die eine Audit-Zeile, die jede erfolgreiche Aktion schreiben muss."""
        logs = [o for o in self.added if isinstance(o, models.ModerationAction)]
        assert len(logs) == 1
        return logs[0]

    def commit(self):
        self.committed = True


def make_user(id, role="user", banned_until=None):
    return models.User(id=id, username=f"u{id}", role=role, banned_until=banned_until,
                       email=f"u{id}@test.de", passwort="x")


def do_ban(actor_role, target_role="user", days=7, target_id=2):
    actor = make_user(1, actor_role)
    target = make_user(target_id, target_role)
    db = FakeDB(target if target_id != 1 else actor)
    result = ban_user(id=target_id, ban=schemas.BanCreate(days=days, reason="Spam"),
                      db=db, current_user=actor)
    return result, target, db


def assert_http(status_code, fn, *args, **kwargs):
    with pytest.raises(HTTPException) as e:
        fn(*args, **kwargs)
    assert e.value.status_code == status_code
    return e.value


# --- Was erlaubt ist --------------------------------------------------------

def test_mod_sperrt_user_7_tage():
    result, target, db = do_ban("moderator", days=7)
    erwartet = datetime.now(timezone.utc) + timedelta(days=7)
    assert abs(target.banned_until - erwartet) < timedelta(seconds=5)
    assert target.ban_reason == "Spam"
    assert db.tokens_deleted and db.committed
    assert result["user_id"] == 2

    log = db.logged()
    assert (log.action, log.moderator_id, log.target_user_id) == ("ban", 1, 2)
    assert log.reason == "Spam" and log.extra == {"days": 7}


def test_mod_darf_genau_bis_zur_obergrenze():
    do_ban("moderator", days=MOD_MAX_BAN_DAYS)


def test_admin_sperrt_dauerhaft():
    _, target, db = do_ban("admin", days=None)
    assert target.banned_until == PERMANENT_BAN_UNTIL
    assert db.logged().extra == {"days": None}


def test_verbotene_sperre_schreibt_kein_log():
    actor, target = make_user(1, "moderator"), make_user(2, "admin")
    db = FakeDB(target)
    assert_http(403, ban_user, id=2, ban=schemas.BanCreate(days=1, reason="x"),
                db=db, current_user=actor)
    assert db.added == [] and not db.committed


def test_admin_sperrt_moderator():
    do_ban("admin", target_role="moderator")


# --- Was verboten ist -------------------------------------------------------

def test_mod_nicht_dauerhaft():
    e = assert_http(403, do_ban, "moderator", days=None)
    assert "permanently" in e.detail


def test_mod_nicht_ueber_obergrenze():
    assert_http(403, do_ban, "moderator", days=MOD_MAX_BAN_DAYS + 1)


@pytest.mark.parametrize("actor, target", [
    ("moderator", "moderator"),
    ("moderator", "admin"),
    ("admin", "admin"),
])
def test_nur_niedrigere_rolle(actor, target):
    assert_http(403, do_ban, actor, target_role=target)


def test_nicht_sich_selbst():
    assert_http(400, do_ban, "admin", target_id=1)


def test_unbekannter_user_404():
    actor = make_user(1, "admin")
    assert_http(404, ban_user, id=99, ban=schemas.BanCreate(days=1, reason="x"),
                db=FakeDB(None), current_user=actor)


def test_schema_lehnt_0_tage_ab():
    with pytest.raises(ValueError):
        schemas.BanCreate(days=0, reason="x")


# --- Entsperren -------------------------------------------------------------

def test_unban():
    target = make_user(2, banned_until=datetime.now(timezone.utc) + timedelta(days=3))
    target.ban_reason = "Spam"
    db = FakeDB(target)
    unban_user(id=2, db=db, current_user=make_user(1, "moderator"))
    assert target.banned_until is None and target.ban_reason is None
    assert db.committed

    # Das Log muss die ALTE Sperre kennen, obwohl sie am User schon geloescht ist.
    log = db.logged()
    assert log.action == "unban" and log.extra["ban_reason"] == "Spam"
    assert log.extra["was_banned_until"] is not None


def test_unban_nicht_gesperrt_400():
    assert_http(400, unban_user, id=2, db=FakeDB(make_user(2)),
                current_user=make_user(1, "moderator"))


# --- Login ------------------------------------------------------------------

def test_login_gesperrt_403(monkeypatch):
    user = make_user(2, banned_until=datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr("app.utils.verify_password", lambda plain, hashed: True)
    creds = SimpleNamespace(username=user.email, password="pw")
    # __wrapped__: am Rate-Limiter-Dekorator vorbei, der braucht einen echten Request
    e = assert_http(403, login.__wrapped__, request=None, user_credentials=creds, db=FakeDB(user))
    assert "suspended" in e.detail


def test_login_falsches_passwort_verraet_sperre_nicht(monkeypatch):
    user = make_user(2, banned_until=datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr("app.utils.verify_password", lambda plain, hashed: False)
    creds = SimpleNamespace(username=user.email, password="falsch")
    assert_http(401, login.__wrapped__, request=None, user_credentials=creds, db=FakeDB(user))
