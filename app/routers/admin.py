from datetime import datetime, timedelta, timezone
from typing import List, Literal
from fastapi import APIRouter, Depends, status, HTTPException, Query, Response
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload
from .. import schemas, database, models, oauth2
# Jeder Router kennt nur seinen eigenen Bucket -> pro Inhaltstyp die passende Funktion.
from .post import delete_s3_object as delete_post_image
from .story import delete_s3_object as delete_story_image
from .user import delete_s3_object as delete_user_image

# EIN Objekt fuer Router-Ebene UND Routen-Parameter: FastAPI fuehrt dieselbe
# Dependency pro Request nur einmal aus. Zwei getrennte require_role("moderator")-
# Aufrufe waeren zwei verschiedene Funktionen und liefen doppelt.
require_moderator = oauth2.require_role("moderator")
# Zusaetzlich zur Router-Dependency, nur an Admin-Routen.
require_admin = oauth2.require_role("admin")

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    # Gilt fuer JEDE Route hier — so kann man den Schutz nicht vergessen.
    dependencies=[Depends(require_moderator)]
)

MOD_MAX_BAN_DAYS = 30
# "Fuer immer" als Datum: so bleibt die Pruefung in check_not_banned ein
# einfaches "liegt banned_until in der Zukunft?" — kein Sonderfall noetig.
PERMANENT_BAN_UNTIL = datetime(9999, 12, 31, tzinfo=timezone.utc)


def _get_target_below_me(db: Session, user_id: int, current_user: models.User) -> models.User:
    """Ziel-User laden und pruefen, dass man ihn ueberhaupt anfassen darf:
    nicht sich selbst, und nur Leute mit NIEDRIGERER Rolle (Mod -> User,
    Admin -> User/Mod). Sonst koennte ein Mod alle Admins aussperren."""

    # db.get statt query: ist der User in diesem Request schon geladen (z.B. in
    # remove_profile_picture), kommt er aus der Session — kein zweiter DB-Rundweg.
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"user with id {user_id} does not exist")

    if target.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="you cannot moderate yourself")

    if oauth2.ROLE_RANK[target.role] >= oauth2.ROLE_RANK[current_user.role]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="you can only moderate users with a lower role")

    return target


def _log_action(db: Session, moderator: models.User, action: str, target_user_id: int,
                target_id: int = None, reason: str = None, content_snapshot: str = None,
                extra: dict = None):
    """Audit-Zeile anlegen. Bewusst KEIN commit: der Aufrufer committet Aktion
    und Log zusammen — entweder landet beides in der DB oder nichts."""
    db.add(models.ModerationAction(moderator_id=moderator.id, action=action,
                                   target_user_id=target_user_id, target_id=target_id,
                                   reason=reason, content_snapshot=content_snapshot,
                                   extra=extra))


def _prepare_removal(db: Session, current_user: models.User, action: str, owner_id: int,
                     target_id: int, remove: schemas.ContentRemove, report_filter,
                     content_snapshot: str = None):
    """Gemeinsamer Teil aller Loesch-Routen, VOR dem eigentlichen Loeschen aufrufen:
    1. Rangregel gegen den Besitzer (wirft 403/400 -> dann wurde noch nichts angefasst)
    2. offene Meldungen zaehlen — nach dem Loeschen sind sie per CASCADE weg
    3. Log-Zeile anlegen (ohne commit, die Route committet alles zusammen)"""

    _get_target_below_me(db, owner_id, current_user)

    open_reports = db.query(func.count()).select_from(models.Report).filter(
        models.Report.status == "pending", report_filter).scalar()

    extra = {"reports": open_reports}
    if remove.details is not None:
        extra["details"] = remove.details

    _log_action(db, current_user, action, owner_id, target_id=target_id,
                reason=remove.reason, content_snapshot=content_snapshot, extra=extra)


@router.post("/users/{id}/ban", response_model=schemas.BanOut)
def ban_user(id: int, ban: schemas.BanCreate, db: Session = Depends(database.get_dp),
             current_user: models.User = Depends(require_moderator)):

    target = _get_target_below_me(db, id, current_user)

    if current_user.role == "moderator":
        if ban.days is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="only admins can ban permanently")
        if ban.days > MOD_MAX_BAN_DAYS:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"moderators can ban for at most {MOD_MAX_BAN_DAYS} days")

    if ban.days is None:
        target.banned_until = PERMANENT_BAN_UNTIL
    else:
        target.banned_until = datetime.now(timezone.utc) + timedelta(days=ban.days)
    target.ban_reason = ban.reason

    # Alle Sessions beenden: ohne Refresh Token kein neuer Access Token. Der
    # aktuelle Access Token scheitert ab sofort an check_not_banned.
    db.query(models.RefreshToken).filter(models.RefreshToken.user_id == target.id).delete()

    # days=None heisst permanent — so steht es auch im Log.
    _log_action(db, current_user, "ban", target.id, reason=ban.reason,
                extra={"days": ban.days})
    db.commit()

    return {"user_id": target.id, "banned_until": target.banned_until, "ban_reason": target.ban_reason}


@router.delete("/users/{id}/ban", response_model=schemas.BanOut)
def unban_user(id: int, db: Session = Depends(database.get_dp),
               current_user: models.User = Depends(require_moderator)):

    target = _get_target_below_me(db, id, current_user)

    if target.banned_until is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="user is not banned")

    # Alte Sperre mitschreiben, BEVOR sie unten geloescht wird — sonst weiss
    # das Log nicht, was eigentlich aufgehoben wurde.
    _log_action(db, current_user, "unban", target.id,
                extra={"was_banned_until": target.banned_until.isoformat(),
                       "ban_reason": target.ban_reason})

    target.banned_until = None
    target.ban_reason = None
    db.commit()

    return {"user_id": target.id, "banned_until": None, "ban_reason": None}


# =========================================================
# Meldungs-Warteschlange
# =========================================================

# Welche Spalte zeigt auf welches Ziel? Ein User-Report hat alle drei auf NULL
# und wird nur ueber reported_user_id erkannt.
TARGET_COLUMNS = {
    "post": models.Report.post_id,
    "story": models.Report.story_id,
    "comment": models.Report.comment_id,
}


def _lower_roles(current_user: models.User) -> List[str]:
    """Alle Rollen UNTER meiner: Mod -> ["user"], Admin -> ["user", "moderator"].
    Gleiche Regel wie beim Sperren — und so sieht niemand Meldungen gegen sich selbst."""
    my_rank = oauth2.ROLE_RANK[current_user.role]
    return [role for role, rank in oauth2.ROLE_RANK.items() if rank < my_rank]


def _target_of(post_id, story_id, comment_id, reported_user_id):
    if post_id is not None:
        return "post", post_id
    if story_id is not None:
        return "story", story_id
    if comment_id is not None:
        return "comment", comment_id
    return "user", reported_user_id


@router.get("/reports", response_model=List[schemas.ReportQueueItem])
def list_reports(report_status: Literal["pending", "dismissed", "action_taken"] = Query("pending", alias="status"),
                 limit: int = Query(20, ge=1, le=100), skip: int = Query(0, ge=0),
                 db: Session = Depends(database.get_dp),
                 current_user: models.User = Depends(require_moderator)):

    R = models.Report

    # EINE Abfrage, gruppiert nach Ziel + Grund: liefert pro Ziel eine Zeile je
    # Grund ("Post 5 / spam / 10", "Post 5 / inappropriate / 3"). Zu einem Eintrag
    # pro Ziel fassen wir unten in Python zusammen.
    rows = (
        db.query(R.post_id, R.story_id, R.comment_id, R.reported_user_id, R.reason,
                 func.count(),
                 func.min(R.created_at),
                 func.max(R.created_at),
                 func.array_agg(R.details).filter(R.details.isnot(None)))
        .join(models.User, models.User.id == R.reported_user_id)
        .filter(R.status == report_status,
                models.User.role.in_(_lower_roles(current_user)))
        .group_by(R.post_id, R.story_id, R.comment_id, R.reported_user_id, R.reason)
        .all()
    )

    groups = {}
    for post_id, story_id, comment_id, user_id, reason, count, first, last, details in rows:
        key = _target_of(post_id, story_id, comment_id, user_id)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"user_id": user_id, "count": 0, "reasons": {},
                                   "details": [], "first": first, "last": last}
        group["count"] += count
        group["reasons"][reason] = count
        group["details"] += details or []  # array_agg mit FILTER liefert NULL statt []
        group["first"] = min(group["first"], first)
        group["last"] = max(group["last"], last)

    # Meiste Meldungen zuerst, bei Gleichstand die aelteste zuerst.
    ordered = sorted(groups.items(), key=lambda item: (-item[1]["count"], item[1]["first"]))
    page = ordered[skip:skip + limit]
    if not page:
        return []

    # Vorschau nachladen: pro Ziel-Typ EINE Abfrage mit id IN (...), nicht eine pro Eintrag.
    ids = {"post": [], "story": [], "comment": []}
    for (target_type, target_id), _ in page:
        if target_type in ids:
            ids[target_type].append(target_id)

    users = {u.id: u for u in db.query(models.User).filter(
        models.User.id.in_({group["user_id"] for _, group in page})).all()}
    posts = {p.id: p for p in db.query(models.Post).filter(models.Post.id.in_(ids["post"])).all()} if ids["post"] else {}
    stories = {s.id: s for s in db.query(models.Story).filter(models.Story.id.in_(ids["story"])).all()} if ids["story"] else {}
    comments = {c.id: c for c in db.query(models.Comments).filter(models.Comments.id.in_(ids["comment"])).all()} if ids["comment"] else {}

    items = []
    for (target_type, target_id), group in page:
        reported_user = users[group["user_id"]]

        preview = None
        if target_type == "post" and target_id in posts:
            post = posts[target_id]
            preview = {"title": post.title, "content": post.content, "image_url": post.image_url}
        elif target_type == "story" and target_id in stories:
            preview = {"image_url": stories[target_id].image_url}
        elif target_type == "comment" and target_id in comments:
            preview = {"content": comments[target_id].comment}
        elif target_type == "user":
            preview = {"content": reported_user.biography, "image_url": reported_user.profile_picture_url}

        items.append({
            "target_type": target_type,
            "target_id": target_id,
            "reported_user": reported_user,
            "report_count": group["count"],
            "reasons": group["reasons"],
            "details": group["details"],
            "first_reported_at": group["first"],
            "last_reported_at": group["last"],
            "preview": preview,
        })

    return items


@router.post("/reports/resolve", response_model=schemas.ReportResolveOut)
def resolve_reports(resolve: schemas.ReportResolve, db: Session = Depends(database.get_dp),
                    current_user: models.User = Depends(require_moderator)):
    """Erledigt ALLE offenen Meldungen zu einem Ziel auf einmal — der Moderator
    entscheidet ueber den Inhalt, nicht ueber jede Meldung einzeln."""

    R = models.Report
    query = db.query(R).filter(R.status == "pending")
    if resolve.target_type == "user":
        query = query.filter(R.reported_user_id == resolve.target_id,
                             R.post_id.is_(None), R.story_id.is_(None), R.comment_id.is_(None))
    else:
        query = query.filter(TARGET_COLUMNS[resolve.target_type] == resolve.target_id)

    first_report = query.first()
    if first_report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="no pending reports for this target")

    # Gleiche Rang-Regel wie beim Sperren: Meldungen gegen sich selbst oder
    # Gleichrangige/Hoehere darf man nicht wegklicken.
    target = _get_target_below_me(db, first_report.reported_user_id, current_user)

    resolved = query.update({R.status: resolve.status,
                             R.resolved_by: current_user.id,
                             R.resolved_at: func.now()},
                            synchronize_session=False)

    # Bei User-Meldungen ist das Ziel der User selbst -> target_id bleibt leer.
    _log_action(db, current_user, "resolve_reports", target.id,
                target_id=None if resolve.target_type == "user" else resolve.target_id,
                extra={"target_type": resolve.target_type, "status": resolve.status,
                       "reports": resolved})
    db.commit()

    return {"resolved": resolved}


# =========================================================
# Inhalte entfernen
# =========================================================
# Ablauf ueberall gleich: laden (404) -> _prepare_removal (Rangregel, Meldungen
# zaehlen, Log) -> Bild aus S3 -> Zeile loeschen -> EIN commit fuer alles.

@router.delete("/posts/{id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_post(id: int, remove: schemas.ContentRemove, db: Session = Depends(database.get_dp),
                current_user: models.User = Depends(require_moderator)):

    post = db.get(models.Post, id)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"post with id {id} does not exist")

    _prepare_removal(db, current_user, "delete_post", post.owner_id, post.id, remove,
                     models.Report.post_id == post.id,
                     content_snapshot=f"{post.title}\n\n{post.content}")

    delete_post_image(post.image_url, db)
    db.delete(post)  # CASCADE: Votes, Kommentare, Meldungen zum Post
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/comments/{id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_comment(id: int, remove: schemas.ContentRemove, db: Session = Depends(database.get_dp),
                   current_user: models.User = Depends(require_moderator)):

    comment = db.get(models.Comments, id)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"comment with id {id} does not exist")

    _prepare_removal(db, current_user, "delete_comment", comment.user_id, comment.id, remove,
                     models.Report.comment_id == comment.id,
                     content_snapshot=comment.comment)

    db.delete(comment)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/stories/{id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_story(id: int, remove: schemas.ContentRemove, db: Session = Depends(database.get_dp),
                 current_user: models.User = Depends(require_moderator)):

    story = db.get(models.Story, id)
    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"story with id {id} does not exist")

    # Kein Snapshot: Story ist nur ein Bild, und Bilder speichern wir bewusst nicht.
    _prepare_removal(db, current_user, "delete_story", story.owner_id, story.id, remove,
                     models.Report.story_id == story.id)

    delete_story_image(story.image_url, db)
    db.delete(story)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{id}/profile-picture", status_code=status.HTTP_204_NO_CONTENT)
def remove_profile_picture(id: int, remove: schemas.ContentRemove, db: Session = Depends(database.get_dp),
                           current_user: models.User = Depends(require_moderator)):

    user = db.get(models.User, id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"user with id {id} does not exist")
    if user.profile_picture_url is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="user has no profile picture")

    R = models.Report
    # User-Meldungen = alle drei Ziel-Spalten NULL. Die bleiben offen (User existiert
    # weiter, kein CASCADE) — erledigt werden sie ueber /admin/reports/resolve.
    _prepare_removal(db, current_user, "delete_profile_picture", user.id, None, remove,
                     and_(R.reported_user_id == user.id, R.post_id.is_(None),
                          R.story_id.is_(None), R.comment_id.is_(None)))

    delete_user_image(user.profile_picture_url, db)
    user.profile_picture_url = None
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =========================================================
# Audit-Log (nur Admin)
# =========================================================

@router.get("/actions", response_model=List[schemas.ModerationActionOut])
def list_actions(action: schemas.ModerationActionType = None,
                 moderator_id: int = None, target_user_id: int = None,
                 limit: int = Query(50, ge=1, le=200), skip: int = Query(0, ge=0),
                 db: Session = Depends(database.get_dp),
                 current_user: models.User = Depends(require_admin)):
    """Neueste zuerst. Alle Filter optional und kombinierbar, z.B.
    ?moderator_id=5 = "was hat Mod 5 gemacht", ?target_user_id=42 = "was lief gegen User 42"."""

    M = models.ModerationAction
    # joinedload: Moderator und Ziel-User kommen per JOIN in DERSELBEN Abfrage mit.
    query = db.query(M).options(joinedload(M.moderator), joinedload(M.target_user))

    if action is not None:
        query = query.filter(M.action == action)
    if moderator_id is not None:
        query = query.filter(M.moderator_id == moderator_id)
    if target_user_id is not None:
        query = query.filter(M.target_user_id == target_user_id)

    # id als Gleichstand-Brecher: gleiche created_at -> trotzdem stabile Reihenfolge beim Blaettern.
    return query.order_by(M.created_at.desc(), M.id.desc()).offset(skip).limit(limit).all()


# =========================================================
# User verwalten
# =========================================================

RECENT_ACTIONS = 10  # so viele Log-Zeilen zeigt das User-Detail


@router.patch("/users/{id}/role", response_model=schemas.RoleOut)
def change_role(id: int, update: schemas.RoleUpdate, db: Session = Depends(database.get_dp),
                current_user: models.User = Depends(require_admin)):
    """Nur Admin. Rangregel wie ueberall: nie sich selbst, nie andere Admins ->
    der letzte Admin kann nicht verschwinden. Admins entfernt nur scripts/make_admin.py."""

    target = _get_target_below_me(db, id, current_user)

    if target.role == update.role:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="user already has this role")

    if update.role != "user" and target.banned_until is not None \
            and target.banned_until > datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="cannot promote a banned user")

    old_role = target.role
    target.role = update.role
    # Kein Token-Reset noetig: get_current_user liest die Rolle bei JEDEM Request
    # frisch aus der DB -> die neue Rolle gilt ab dem naechsten Aufruf.
    _log_action(db, current_user, "role_change", target.id, reason=update.reason,
                extra={"old_role": old_role, "new_role": update.role})
    db.commit()

    return {"user_id": target.id, "role": target.role}



@router.get("/users/{id}", response_model=schemas.AdminUserDetail)
def get_user_detail(id: int, db: Session = Depends(database.get_dp),
                    current_user: models.User = Depends(require_moderator)):
    """Profil + Bann-Status + Meldungszahlen + letzte Aktionen gegen den User.
    Gleiche Rangregel wie die Report-Queue: niemand schaut sich selbst oder Hoehere an."""

    target = _get_target_below_me(db, id, current_user)

    R = models.Report
    # EINE Abfrage fuer beide Zahlen: count() mit FILTER zaehlt nur die passenden Zeilen.
    reports_pending, reports_total = db.query(
        func.count().filter(R.status == "pending"),
        func.count(),
    ).filter(R.reported_user_id == target.id).one()

    M = models.ModerationAction
    actions = (db.query(M).options(joinedload(M.moderator), joinedload(M.target_user))
               .filter(M.target_user_id == target.id)
               .order_by(M.created_at.desc(), M.id.desc())
               .limit(RECENT_ACTIONS).all())

    return {
        "id": target.id,
        "username": target.username,
        "role": target.role,
        "created_at": target.created_at,
        "biography": target.biography,
        "profile_picture_url": target.profile_picture_url,
        "banned_until": target.banned_until,
        "ban_reason": target.ban_reason,
        "reports_pending": reports_pending,
        "reports_total": reports_total,
        "recent_actions": actions,
    }
