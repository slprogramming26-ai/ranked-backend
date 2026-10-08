from .database import Base
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, UniqueConstraint, ForeignKeyConstraint, Index, CheckConstraint, Enum
from sqlalchemy.sql.sqltypes import TIMESTAMP, DATE
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import null, text
from sqlalchemy.orm import relationship
from .database import Base


class Location(Base):
    __tablename__ = 'locations'

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String, nullable=False, unique=True)



class Post(Base):
    __tablename__ = 'posts'

    id = Column(Integer, primary_key= True, nullable= False)
    title = Column(String, nullable= False)
    content = Column(String, nullable= False)
    created_at = Column(TIMESTAMP(timezone=True), nullable= False, server_default= text('now()'), index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable = False, index=True)
    image_url = Column(String, nullable=True)
    flag = Column(String, nullable= True)  # "engagement" | "creativity" | "productivity" | None — Punkte-Multiplikator
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    # Denormalisierter Vote-Zaehler: wird beim Voten fortgeschrieben (siehe vote.py),
    # damit der Feed nicht mehr per GROUP BY ueber die votes-Tabelle aggregieren muss.
    # server_default '0': bestehende Zeilen bekommen sofort einen gueltigen Wert.
    vote_count = Column(Integer, nullable=False, server_default=text('0'))

    owner = relationship("User")
    location = relationship("Location")


class User(Base):

    __tablename__ = 'users'

    id = Column(Integer, primary_key= True, nullable= False)
    email = Column(String, nullable=False, unique= True)
    passwort = Column(String, nullable= False)
    username = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable= False, server_default= text('now()'))
    vibe_factor_1 = Column(String, nullable= True)
    vibe_factor_2 = Column(String, nullable= True)
    biography = Column(String, nullable= True)
    profile_picture_url = Column(String, nullable= True)
    ranking_enabled = Column(Boolean, server_default='False', nullable=False)
    xp = Column(Integer, nullable=False, server_default=text('0'))
    streak_count = Column(Integer, nullable=False, server_default=text('0'))
    last_swipe_date = Column(DATE, nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    location = relationship("Location")
    # für admin bereich relevant
    role = Column(Enum("user", "moderator", "admin", name="user_roles"), nullable=False, server_default="user")
    banned_until = Column(TIMESTAMP(timezone=True), nullable=True)
    ban_reason = Column(String, nullable=True)


class FailedImageDeletions(Base):

    __tablename__ = 'failed_image_deletions'

    id = Column(Integer, primary_key= True, nullable= False)
    bucket = Column(String, nullable=False)
    s3_key = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable= False, server_default= text('now()'))






class Votes(Base):

    __tablename__ = 'votes'
    
    user_id = Column(Integer,ForeignKey("users.id", ondelete="CASCADE"), primary_key= True,)
    # index=True: der PK (user_id, post_id) hilft nur bei Suchen nach user_id (erste Spalte).
    # Der Feed sucht aber nach post_id -> braucht einen eigenen Index.
    post_id = Column(Integer,ForeignKey("posts.id", ondelete="CASCADE"), primary_key= True, index=True)


class Comments(Base):

    __tablename__ = 'comments'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer,ForeignKey("users.id", ondelete="CASCADE"),)
    post_id = Column(Integer,ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    comment = Column(String, nullable=False)

class DailyTarget(Base):
    __tablename__ = 'daily_targets'

    id = Column(Integer, primary_key=True, nullable=False)
    voter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True) # Wer schaut?
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False) # Wer wird bewertet?
    date = Column(DATE, nullable=False, server_default=text('now()'))
    
    voter = relationship("User", foreign_keys=[voter_id])
    target_user = relationship("User", foreign_keys=[target_user_id])


class RankingScores(Base):

    __tablename__ = 'ranking_scores'

    id = Column(Integer, primary_key=True, nullable=False)
    voter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)

    direction = Column(Boolean, nullable=False)  # True = Rechts/Cool, False = Links/Nicht cool
    points = Column(Integer, nullable=False)      # vom Server berechnet aus post.flag + direction

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)

    post = relationship("Post")



class DailyBonusLog(Base):
    __tablename__ = 'daily_bonus_log'

    date = Column(DATE, primary_key=True, nullable=False)
    processed_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    awarded_count = Column(Integer, nullable=False, server_default=text('0'))


class Activity(Base):
    __tablename__ = 'activities'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)  # Empfänger
    type = Column(String, nullable=False)  # "rated" (später auch "placement"/"streak"/"badge")
    payload = Column(Integer, nullable=False)  # z.B. erhaltene Punkte bei "rated"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    # Activity-Liste: nach Empfaenger filtern, nach Zeit sortieren (neueste 30).
    __table_args__ = (
        Index('ix_activities_user_id_created_at', 'user_id', 'created_at'),
    )



class Follows(Base):

    __tablename__ = 'follows'

    follower_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,  primary_key= True)
    # index=True: der PK (follower_id, followee_id) hilft nur bei Suchen nach follower_id.
    # Der Follower-Count sucht nach followee_id -> braucht einen eigenen Index.
    followee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,  primary_key= True, index=True)





class Message(Base):
   

    __tablename__ = 'message'

    id = Column(Integer, primary_key=True, nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    # Vom Client erzeugte UUID pro Nachricht (Idempotenz-/Dedup-Key).
    # nullable=True, weil Altbestand keinen Key hat.
    client_msg_id = Column(String, nullable=True)

    # Zusammengesetzte Indexe fuer den Chat-Sync: erst nach Person filtern,
    # dann nach Zeit sortieren/eingrenzen — genau die Query in GET /messages.
    __table_args__ = (
        Index('ix_message_recipient_id_created_at', 'recipient_id', 'created_at'),
        Index('ix_message_sender_id_created_at', 'sender_id', 'created_at'),
        # Idempotenz serverseitig ERZWINGEN: dieselbe client_msg_id darf pro
        # Absender nur einmal existieren (Reconnect-Race -> zweiter Insert knallt,
        # der Manager behandelt das als "gibt es schon" statt neu zu speichern).
        # NULL-Altbestand kollidiert nicht: NULLs gelten im Unique-Index als verschieden.
        Index('uq_message_sender_client_msg_id', 'sender_id', 'client_msg_id', unique=True),
    )


class GroupChats(Base):
    

    __tablename__ = 'group_chats'

    group_chat_id = Column(Integer, primary_key=True, nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    group_name = Column(String, nullable=True)
    profile_picture = Column(String, nullable=True)

    # "dirty"-Flag für Lazy Rekeying: Wird bei jeder Mitglieder-Änderung (Beitritt/
    # Verlassen/Kick) auf True gesetzt. Der nächste Sender erzeugt dann eine neue
    # Schlüssel-Epoche, verteilt sie und setzt das Flag zurück auf False.
    needs_rekey = Column(Boolean, nullable=False, server_default='False')

class GroupChatJoinCodes(Base):

    __tablename__ = 'group_chats_join_codes'

    id = Column(Integer, primary_key=True, nullable=False)
    code = Column(Integer, nullable=False, unique=True)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))



class GroupChatMembership(Base):

    __tablename__ = 'group_chat_memberships'

    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False, primary_key=True)
    # index=True: "meine Gruppen" sucht nach participant_id (zweite PK-Spalte) -> eigener Index.
    participant_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True, index=True)
    joined_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class GroupMessage(Base):

    __tablename__ = 'group_message'

    id = Column(Integer, primary_key=True, nullable=False)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    client_msg_id = Column(String, nullable=True)
    # Mit welcher Schlüssel-Epoche der Gruppe diese Nachricht verschlüsselt wurde.
    key_version = Column(Integer, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ['group_chat_id', 'key_version'],
            ['group_chat_epochs.group_chat_id', 'group_chat_epochs.key_version'],
            ondelete='CASCADE',
        ),
        # Chat-Sync der Gruppe: erst nach Gruppe filtern, dann nach Zeit.
        Index('ix_group_message_group_chat_id_created_at', 'group_chat_id', 'created_at'),
        # Idempotenz wie bei Message: eine client_msg_id pro Absender nur einmal.
        Index('uq_group_message_sender_client_msg_id', 'sender_id', 'client_msg_id', unique=True),
    )




class GroupChatEpoch(Base):
    __tablename__ = 'group_chat_epochs'
    #epoche ist das akutelle schloss für gruppe group_chat id mit bestimmte schlüssel version
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), primary_key=True, nullable=False)
    key_version = Column(Integer, primary_key=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Block(Base):
    __tablename__ = 'blocks'
    blocker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # index=True: der Chat prueft Blocks in BEIDE Richtungen, der PK deckt nur blocker_id ab.
    blocked_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Report(Base):
    __tablename__ = 'reports'
    id = Column(Integer, primary_key=True, nullable=False)
    # index=True: Feed/Stories/Comments fragen bei JEDEM Abruf "was habe ICH gemeldet?" ab.
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    reported_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Was wurde gemeldet? target_id bewusst OHNE ForeignKey: loescht der User seinen
    # Post, muss die Meldung trotzdem stehen bleiben (sonst: loeschen, neu hochladen, nie erwischt).
    # Bei 'user' ist target_id NULL, das Ziel ist dann reported_user_id.
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=True)
    # Text-Kopie beim Melden (Post: Titel + Text, Kommentar: Text). Beleg, falls der Inhalt
    # geloescht oder bearbeitet wird. Bilder bewusst nicht. Wird per Cleanup geleert.
    content_snapshot = Column(String, nullable=True)
    # Feste Kategorie (Liste in schemas.ReportReason), damit die Moderation pro Ziel
    # sauber zaehlen kann ("Spam: 10, Belaestigung: 3"). Freitext gehoert in details.
    reason = Column(String, nullable=False)
    details = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default=text("'pending'")) # pending / dismissed / action_taken
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    # Wer hat erledigt und wann. SET NULL: wird der Moderator geloescht, bleibt die Meldung erhalten.
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("reason IN ('spam', 'harassment', 'inappropriate', 'misinformation', 'other')",
                        name="ck_reports_reason"),
        CheckConstraint("status IN ('pending', 'dismissed', 'action_taken')", name="ck_reports_status"),
        CheckConstraint("target_type IN ('post', 'story', 'comment', 'user')", name="ck_reports_target_type"),
        # 'user' <=> kein target_id. Verhindert halbe Zeilen wie ('post', NULL).
        CheckConstraint("(target_type = 'user') = (target_id IS NULL)", name="ck_reports_target_id"),
        # Resolve + "Meldungen zu diesem Ziel" suchen nach genau diesem Paar.
        Index('ix_reports_target_type_target_id', 'target_type', 'target_id'),
    )


class ModerationAction(Base):
    """Audit-Log: jede Mod/Admin-Aktion schreibt eine Zeile, im SELBEN Commit wie
    die Aktion selbst. Entweder passiert beides oder nichts."""

    __tablename__ = 'moderation_actions'

    id = Column(Integer, primary_key=True, nullable=False)
    # SET NULL bei beiden: loescht Mod oder betroffener User seinen Account, bleibt
    # die Zeile erhalten (Statistik), ist aber nicht mehr personenbezogen.
    moderator_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # index=True: "was lief alles gegen User X?" (User-Detail in Phase 6)
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String, nullable=False)
    # Post/Kommentar/Story-ID. Bewusst KEIN FK: das Ziel wird ja oft geloescht,
    # mit CASCADE verschwaende die Log-Zeile gleich mit.
    target_id = Column(Integer, nullable=True)
    reason = Column(String, nullable=True)
    # Text-Kopie des geloeschten Inhalts als Beleg bei Einspruch. DELETE /cleanup/moderation_snapshots
    # (taeglich) leert sie nach 6 Monaten oder sobald target_user_id NULL ist (Account geloescht).
    content_snapshot = Column(String, nullable=True)
    # Was je nach Aktion dazugehoert: {"days": 7}, {"old_role": ..., "new_role": ...}, {"reports": 12}
    extra = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    # foreign_keys noetig: zwei FKs auf users, SQLAlchemy wuesste sonst nicht, welcher welcher ist.
    moderator = relationship("User", foreign_keys=[moderator_id])
    target_user = relationship("User", foreign_keys=[target_user_id])

    __table_args__ = (
        CheckConstraint(
            "action IN ('ban', 'unban', 'resolve_reports', 'delete_post', 'delete_comment', "
            "'delete_story', 'delete_profile_picture', 'role_change', 'restore_content')",

            name="ck_moderation_actions_action"),
    )


class RemovedContent(Base):
    """Archiv fuer von Mods entfernte Inhalte. Die Originalzeile wird geloescht (keine
    bestehende Abfrage braucht einen Filter), hier liegt eine Kopie fuer Widerspruch
    und Wiederherstellung. Nach 180 Tagen raeumt DELETE /cleanup/removed_content auf."""

    __tablename__ = 'removed_content'

    id = Column(Integer, primary_key=True, nullable=False)
    target_type = Column(String, nullable=False)
    # Bewusst KEIN FK: die Originalzeile existiert ja nicht mehr. NULL bei profile_picture.
    target_id = Column(Integer, nullable=True)
    # CASCADE: loescht der User seinen Account, ist auch sein Archiv weg (DSGVO).
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Bruecke zum Audit-Log: Restore laeuft ueber die Action-ID. unique = eine Aktion, ein Archiv.
    moderation_action_id = Column(Integer, ForeignKey("moderation_actions.id", ondelete="SET NULL"),
                                  nullable=True, unique=True)
    # Eigene Spalte, damit Cron und delete_account das S3-Bild finden, ohne JSON zu parsen.
    image_url = Column(String, nullable=True)
    # Komplette alte Zeile(n) per to_jsonb, Restore schreibt sie mit jsonb_populate_record zurueck.
    # Bei Posts: {"post": ..., "comments": [...], "votes": [...], "ranking_scores": [...]}
    data = Column(JSONB, nullable=False)
    # index=True: der Cron sucht "aelter als 180 Tage"
    removed_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)

    __table_args__ = (
        CheckConstraint(
            "target_type IN ('post', 'comment', 'story', 'profile_picture')",
            name="ck_removed_content_target_type"),
    )



class UserKey(Base):
    __tablename__ = 'user_keys'

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    public_key = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class KeyBackup(Base):
    __tablename__ = 'key_backups'

    # Eine Zeile pro User: sein privater Schlüssel, vom Client mit einem Passwort
    # verschlüsselt. Der Server sieht nie Klartext-Key oder Passwort, macht keine Krypto.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Womit das Backup geschützt ist: 'login_password' oder 'custom'
    secret_type = Column(String, nullable=False)
    salt = Column(String, nullable=False)
    nonce = Column(String, nullable=False)
    ciphertext = Column(String, nullable=False)
    # Argon2-Parameter, vom Client gewählt. Sie werden mitgespeichert, damit alte
    # Backups lesbar bleiben, wenn wir die Werte später erhöhen.
    opslimit = Column(Integer, nullable=False)
    memlimit = Column(Integer, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    __table_args__ = (
        CheckConstraint("secret_type IN ('login_password', 'custom')", name="ck_key_backups_secret_type"),
    )



class GroupChatKey(Base):
    __tablename__ = 'group_chat_keys'

    # Eine Zeile = die verschlüsselte Kopie des Gruppenschlüssels (Version key_version)
    # der Gruppe group_chat_id, bestimmt für das Mitglied recipient_id.
    id = Column(Integer, primary_key=True, nullable=False)
    group_chat_id = Column(Integer, ForeignKey("group_chats.group_chat_id", ondelete="CASCADE"), nullable=False)
    # Epoche des Gruppenschlüssels. Steigt bei jedem Rekey (Beitritt/Kick/Verlassen) um 1.
    # Eine neue Version erhält nur, wer zu diesem Zeitpunkt Mitglied ist -> keine History.
    key_version = Column(Integer, nullable=False)
    # Für welches Mitglied diese Kopie bestimmt ist.
    recipient_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Der symmetrische Gruppenschlüssel, verschlüsselt mit dem Public Key des recipient.
    # Der Server sieht den Klartext-Key nie.
    encrypted_key = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    # Pro Mitglied genau eine Kopie je Version.
    # Zusätzlich: zusammengesetzter FK auf die Epochen-Tabelle, damit keine Schlüssel-
    # Kopie auf eine nicht existierende Epoche zeigen kann.
    __table_args__ = (
        UniqueConstraint('group_chat_id', 'key_version', 'recipient_id', name='uq_group_key_version_recipient'),
        ForeignKeyConstraint(
            ['group_chat_id', 'key_version'],
            ['group_chat_epochs.group_chat_id', 'group_chat_epochs.key_version'],
            ondelete='CASCADE',
        ),
    )




class RefreshToken(Base):
    __tablename__ = 'refresh_tokens'

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Wir speichern NICHT den Token selbst, sondern seinen Hash (SHA-256).
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class Story(Base):
    __tablename__ = 'stories'

    id = Column(Integer, primary_key=True, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    image_url = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)

    owner = relationship("User")


class FeedImpression(Base):
    """Trainingsdaten fuer Lytir: welcher Post wurde wem im Feed WIRKLICH gezeigt.

    Die votes-/ranking_scores-Tabellen kennen nur Reaktionen. Was fehlt, ist das
    Gegenbeispiel — "gesehen und ignoriert". Ohne diese Zeilen hat das Modell nur
    positive Beispiele und lernt nichts Unterscheidbares.

    Der Client schickt KUMULATIV: dieselbe (feed_session_id, post_id) kommt bei
    jedem Flush erneut, mit gewachsenem dwell_ms. Das ist kein Retry-Fehlerfall,
    sondern das normale Verhalten -> der Endpunkt macht ein Upsert auf dem
    Unique-Index unten, kein blindes Insert.

    Beta-Tabelle: sie wird nur geschrieben und einmal zum Export gelesen.
    """

    __tablename__ = 'feed_impressions'

    id = Column(Integer, primary_key=True, nullable=False)
    # Kommt aus dem Token, nie aus dem Body.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)

    # Vom Client erzeugte UUID pro Feed-Sitzung (neu bei Initial-Load, Pull-to-Refresh
    # und Feed-Wechsel, NICHT bei Pagination). Als String wie client_msg_id bei Message.
    feed_session_id = Column(String, nullable=False)
    feed_variant = Column(String, nullable=False)          # "local" | "for_you"

    # 0-basiert, beim ERSTEN Sichtbarwerden festgehalten und danach nie geaendert.
    # Braucht man beim Training gegen den Position-Bias: Platz 0 wird immer mehr
    # gesehen als Platz 9, unabhaengig davon wie gut der Post ist.
    position = Column(Integer, nullable=False)
    shown_at = Column(TIMESTAMP(timezone=True), nullable=False)   # Client-Uhr
    # Summe aller Sichtbarkeitsphasen. Untergrenze 1000 (darunter sendet der Client
    # gar nicht), Obergrenze 180000 — genau 180000 heisst "gekappt", nicht gemessen.
    dwell_ms = Column(Integer, nullable=False)

    # Vier getrennte Signale, bewusst NICHT zu einem "reacted" zusammengefaltet:
    # reported ist negativ, die anderen drei sind positiv. Ein ODER ueber alle vier
    # wuerde dem Modell beibringen, gemeldete Posts oefter auszuspielen.
    voted = Column(Boolean, nullable=False, server_default=text('false'))
    opened_comments = Column(Boolean, nullable=False, server_default=text('false'))
    shared = Column(Boolean, nullable=False, server_default=text('false'))
    reported = Column(Boolean, nullable=False, server_default=text('false'))

    # Snapshot der ROHWERTE (FeatureInput), nicht des fertigen Vektors: so laesst
    # sich build_features() spaeter in jeder Version neu drueberlaufen lassen.
    # Wird nur beim INSERT gesetzt und beim Upsert NIE ueberschrieben — der erste
    # Flush liegt am naechsten am tatsaechlichen Anzeigezeitpunkt.
    # nullable, damit ein inzwischen geloeschter Post nicht den ganzen Batch kippt.
    features = Column(JSONB, nullable=True)
    feature_version = Column(Integer, nullable=True)

    received_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    __table_args__ = (
        # Konflikt-Ziel des Upserts. user_id ist bewusst Teil des Keys: die
        # feed_session_id kommt vom Client, ohne user_id koennte ein fremder Client
        # mit geratener Session-ID die Zeilen eines ANDEREN Users ueberschreiben.
        # Gleiches Muster wie uq_message_sender_client_msg_id.
        Index(
            'uq_feed_impression_user_session_post',
            'user_id', 'feed_session_id', 'post_id',
            unique=True,
        ),
    )





