"""Sucht in der DB brauchbare IDs fuer die internen WS-Tests. NUR LESEND.

Aufruf:  python -m scripts.ws_discover
"""
from sqlalchemy import func
from app.database import SessionLocal
from app import models

db = SessionLocal()

# --- Kandidat fuer Test 6: Gruppe, die sauber ist UND schon eine Epoche hat. ---
# Nur dann kommt bei falscher key_version ein "key_outdated" heraus.
# Ist die Gruppe dirty oder ohne Epoche, faengt RekeyRequired vorher ab.
newest_epoch = (
    db.query(
        models.GroupChatEpoch.group_chat_id.label("gid"),
        func.max(models.GroupChatEpoch.key_version).label("kv"),
    )
    .group_by(models.GroupChatEpoch.group_chat_id)
    .subquery()
)

rows = (
    db.query(
        models.GroupChats.group_chat_id,
        models.GroupChats.needs_rekey,
        newest_epoch.c.kv,
        func.count(models.GroupChatMembership.participant_id).label("mitglieder"),
    )
    .join(newest_epoch, newest_epoch.c.gid == models.GroupChats.group_chat_id)
    .join(
        models.GroupChatMembership,
        models.GroupChatMembership.group_chat_id == models.GroupChats.group_chat_id,
    )
    .filter(models.GroupChats.needs_rekey.is_(False))
    .group_by(
        models.GroupChats.group_chat_id,
        models.GroupChats.needs_rekey,
        newest_epoch.c.kv,
    )
    .order_by(func.count(models.GroupChatMembership.participant_id).desc())
    .limit(5)
    .all()
)

print("=== Gruppen (sauber, mit Epoche) — brauchbar fuer Test 6 ===")
if not rows:
    print("  KEINE gefunden. Test 6 (key_outdated) so nicht moeglich —")
    print("  jede Gruppe ist entweder needs_rekey=True oder hat keine Epoche.")
for gid, dirty, kv, anzahl in rows:
    mitglieder = (
        db.query(models.GroupChatMembership.participant_id)
        .filter(models.GroupChatMembership.group_chat_id == gid)
        .all()
    )
    ids = [m[0] for m in mitglieder]
    print(f"  group_chat_id={gid}  aktuelle key_version={kv}  mitglieder={ids}")

# --- Kandidaten fuer die DM-Tests: zwei User ohne Block-Beziehung. ---
print()
print("=== Zuletzt angelegte User (fuer die DM-Tests) ===")
users = (
    db.query(models.User.id, models.User.username)
    .order_by(models.User.id.desc())
    .limit(8)
    .all()
)
for uid, name in users:
    print(f"  id={uid}  username={name}")

print()
print("=== Bestehende Blocks (diese Paare NICHT als sender/recipient nehmen) ===")
blocks = db.query(models.Block.blocker_id, models.Block.blocked_id).limit(20).all()
if not blocks:
    print("  keine")
for a, b in blocks:
    print(f"  {a} <-> {b}")

db.close()
