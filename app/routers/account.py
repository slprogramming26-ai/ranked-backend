from fastapi import Response, status, HTTPException, Depends, APIRouter, Request, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import timedelta, datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert
import redis
from .. import models, schemas, oauth2, utils
from ..database import get_dp
from ..limiter import limiter
from ..mail import send_mail
from ..redis_client import redis_client


# E-Mail-Codes: bestaetigen, neu anfordern, Passwort vergessen.
# Gleiches Praefix wie user.py -> die URLs bleiben /users/verify, /users/password/... .
# Registrieren (POST /users/) steht weiter in user.py und holt sich issue_email_code von hier.
router = APIRouter(
    prefix="/users",
    tags=['Account']
)


EMAIL_CODE_LIFETIME = timedelta(minutes=15)
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_CODE_RESEND_COOLDOWN = timedelta(seconds=60)
PW_RESET_MAX_FAILS = 10
PW_RESET_FAIL_WINDOW = timedelta(hours=24)

def _pw_reset_fail_key(user_id: int) -> str:
    return f"pw_reset_fail:{user_id}"


def get_pw_reset_fails(user_id: int) -> int:
    # Redis weg -> 0 (fail open): die 5 Versuche pro Code schuetzen trotzdem weiter.
    try:
        value = redis_client.get(_pw_reset_fail_key(user_id))
    except redis.RedisError as e:
        print(f"Warnung: pw_reset_fail lesen fehlgeschlagen fuer user {user_id}: {e}")
        return 0
    return int(value) if value else 0

def record_pw_reset_fail(user_id: int) -> None:
    # incr + expire in EINER Pipeline -> nie ein Zaehler ohne Ablaufzeit.
    try:
        pipe = redis_client.pipeline()
        pipe.incr(_pw_reset_fail_key(user_id))
        pipe.expire(_pw_reset_fail_key(user_id), PW_RESET_FAIL_WINDOW)
        pipe.execute()
    except redis.RedisError as e:
        print(f"Warnung: pw_reset_fail schreiben fehlgeschlagen fuer user {user_id}: {e}")

def clear_pw_reset_fails(user_id: int) -> None:
    try:
        redis_client.delete(_pw_reset_fail_key(user_id))
    except redis.RedisError as e:
        print(f"Warnung: pw_reset_fail loeschen fehlgeschlagen fuer user {user_id}: {e}")




def issue_email_code(db: Session, user_id: int, purpose: str) -> str:
    # Legt den Code an oder überschreibt den alten (ein Code pro User + Zweck).
    # Kein commit hier: der Aufrufer committet, damit alles in EINER Transaktion landet.
    code = utils.generate_code()
    now = datetime.now(timezone.utc)

    stmt = pg_insert(models.EmailCode).values(
        user_id=user_id,
        purpose=purpose,
        code_hash=utils.hash_code(code),
        attempts=0,
        expires_at=now + EMAIL_CODE_LIFETIME,
        created_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "purpose"],
        set_={
            "code_hash": stmt.excluded.code_hash,
            "attempts": 0,
            "expires_at": stmt.excluded.expires_at,
            "created_at": stmt.excluded.created_at,
        },
    )
    db.execute(stmt)
    return code



@router.post("/verify", response_model=schemas.Token)
@limiter.limit("10/minute")
def verify_email(request: Request, data: schemas.EmailVerify, db: Session = Depends(get_dp)):

    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Code ungültig oder abgelaufen."
    )

    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise invalid

    if user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="E-Mail ist bereits bestätigt."
        )

    email_code = db.query(models.EmailCode).filter(
        models.EmailCode.user_id == user.id,
        models.EmailCode.purpose == "verify",
    ).with_for_update().first()
    if not email_code:
        raise invalid

    if email_code.expires_at < datetime.now(timezone.utc):
        raise invalid

    if email_code.attempts >= EMAIL_CODE_MAX_ATTEMPTS:
        raise invalid

    if not utils.verify_code(data.code, email_code.code_hash):
        email_code.attempts += 1
        db.commit()
        raise invalid



    oauth2.check_not_banned(user)

    user.email_verified = True
    db.delete(email_code)


    access_token = oauth2.create_access_token(data={"user_id": str(user.id)})
        
    # create_refresh_token committet -> email_verified + Code-Löschung landen mit drin
    refresh_token = oauth2.create_refresh_token(user_id=user.id, db=db)
        
    return {"access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer"
        }


@router.post("/verify/resend", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/minute")
def resend_verification(request: Request, data: schemas.EmailResend, background_tasks: BackgroundTasks, db: Session = Depends(get_dp)):

    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user or user.email_verified:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    email_code = db.query(models.EmailCode).filter(
        models.EmailCode.user_id == user.id,
        models.EmailCode.purpose == "verify",
    ).first()

    if email_code and email_code.created_at > datetime.now(timezone.utc) - EMAIL_CODE_RESEND_COOLDOWN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Bitte warte kurz, bevor du einen neuen Code anforderst."
        )

    code = issue_email_code(db, user.id, "verify")
    db.commit()

    background_tasks.add_task(
        send_mail,
        user.email,
        "Dein Ranked-Code",
        f"Dein Bestätigungscode: {code}\nEr ist 15 Minuten gültig.",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)



@router.post("/password/forgot", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/minute")
def forgot_password(request: Request, data: schemas.PasswordForgot, background_tasks: BackgroundTasks, db: Session = Depends(get_dp)):

    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    email_code = db.query(models.EmailCode).filter(
        models.EmailCode.user_id == user.id,
        models.EmailCode.purpose == "reset",
    ).first()

    if email_code and email_code.created_at > datetime.now(timezone.utc) - EMAIL_CODE_RESEND_COOLDOWN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Bitte warte kurz, bevor du einen neuen Code anforderst."
        )

    code = issue_email_code(db, user.id, "reset")
    db.commit()

    background_tasks.add_task(
        send_mail,
        user.email,
        "Passwort zurücksetzen",
        f"Dein Code zum Zurücksetzen: {code}\nEr ist 15 Minuten gültig.\n"
        "Wenn du das nicht angefordert hast, ignoriere diese Mail – dein Passwort bleibt unverändert.",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
def reset_password(request: Request, data: schemas.PasswordReset, db: Session = Depends(get_dp)):

    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Code ungültig oder abgelaufen."
    )

    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise invalid

    # Tagesbremse VOR der Code-Abfrage: wer 10x falsch geraten hat, kommt gar nicht erst an die DB-Zeile
    if get_pw_reset_fails(user.id) >= PW_RESET_MAX_FAILS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zu viele Fehlversuche. Bitte versuche es morgen erneut."
        )

    email_code = db.query(models.EmailCode).filter(
        models.EmailCode.user_id == user.id,
        models.EmailCode.purpose == "reset",
    ).with_for_update().first()
    if not email_code:
        raise invalid

    if email_code.expires_at < datetime.now(timezone.utc):
        raise invalid

    if email_code.attempts >= EMAIL_CODE_MAX_ATTEMPTS:
        raise invalid

    if not utils.verify_code(data.code, email_code.code_hash):
        email_code.attempts += 1
        db.commit()
        record_pw_reset_fail(user.id)
        raise invalid

    # Code stimmt -> neues Passwort
    user.passwort = utils.hash_password(data.new_password)
    # Code kam an diese Adresse -> Postfach gehört ihm, also gleich mit verifizieren
    user.email_verified = True

    # ALLE Codes des Users weg (reset + evtl. noch offener verify-Code)
    db.query(models.EmailCode).filter(models.EmailCode.user_id == user.id).delete()

    # Alle Sitzungen beenden (wie beim Bann in admin.py) -> wer das alte Passwort kannte, fliegt raus
    db.query(models.RefreshToken).filter(models.RefreshToken.user_id == user.id).delete()

    db.commit()
    clear_pw_reset_fails(user.id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
