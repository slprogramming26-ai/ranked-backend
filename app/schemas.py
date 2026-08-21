from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from typing import Optional, Literal, List




# =========================================================
# Locations
# =========================================================

class LocationOut(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class LocationBulkCreate(BaseModel):
    names: List[str]



# =========================================================
# User & Auth
# =========================================================

class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    created_at: datetime
    ranking_enabled: bool
    profile_picture_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    email: EmailStr
    username: str
    passwort: str
    age: int = Field(ge=0, le=120)


class UserDetails(BaseModel):
    vibe_factor_1: Optional[str] = None
    vibe_factor_2: Optional[str] = None
    profile_picture_url: Optional[str] = None
    biography: Optional[str] = None
    ranking_enabled: Optional[bool] = None
    location_id: Optional[int] = None



class LeagueOut(BaseModel):
    """Liga-Fortschritt, berechnet aus der XP-Summe (siehe xp_config.get_league).
    Genug fuer Ring + Fortschrittsbalken: 'Gold · 400/2000 · noch 1600 bis Platin'."""
    league: str
    tier: int
    next_league: Optional[str] = None
    xp_into_league: int
    league_span: int
    xp_for_next: int



class GetUserOut(BaseModel):
    id: int
    email: Optional[EmailStr] = None
    username: str
    vibe_factor_1: Optional[str] = None
    vibe_factor_2: Optional[str] = None
    profile_picture_url: Optional[str] = None
    biography: Optional[str] = None
    location: Optional[LocationOut] = None
    ranking_enabled: bool
    follower_count: Optional[int] = None
    is_followed: Optional[bool] = None
    # Gamification. Optional, weil /users/search dieselbe Schema-Klasse nutzt,
    # dort aber (bewusst) keine Liga/XP mitliefert -> bleibt dann None.
    xp: Optional[int] = None
    streak_count: Optional[int] = None
    league: Optional[LeagueOut] = None

    model_config = ConfigDict(from_attributes=True)


class UserLogin(BaseModel):
    email: EmailStr
    passwort: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenData(BaseModel):
    id: int






# =========================================================
# Posts
# =========================================================

class PostBase(BaseModel):
    title: str
    content: str


class PostCreate(PostBase):
    image_url: Optional[str] = None
    flag: Optional[Literal["engagement", "creativity", "productivity"]] = None
    location_id: Optional[int] = None


class Post(PostBase):
    id: int
    created_at: datetime
    owner_id: int
    owner: UserOut
    image_url: Optional[str] = None
    flag: Optional[str] = None
    location: Optional[LocationOut] = None


    model_config = ConfigDict(from_attributes=True)


class PostOut(BaseModel):
    post: Post
    votes: int
    is_mine: bool
    is_liked: bool
    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Stories
# =========================================================

class StoryOut(BaseModel):
    id: int
    owner_id: int
    image_url: str
    created_at: datetime
    owner: UserOut
    is_mine: bool

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Votes & Follows
# =========================================================

class Vote(BaseModel):
    post_id: int
    dir: int = Field(le=1)


class Follow(BaseModel):
    followee_id: int
    dir: int = Field(le=1)


# =========================================================
# Reports
# =========================================================

class ReportCreate(BaseModel):
    """Was der Melder schickt: nur den Grund. WAS gemeldet wird, steht im URL-Pfad
    (/report/post/{id} usw.), den Rest (Owner etc.) leitet der Server selbst ab."""
    reason: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")


# =========================================================
# Comments
# =========================================================

class CommentBase(BaseModel):
    post_id: int
    comment: str


class CreateComment(CommentBase):
    pass


class CommentOut(BaseModel):
    id: int  # braucht das Frontend, um den Kommentar melden zu können (/report/comment/{id})
    username: str
    post_id: int
    comment: str

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Ranking / Swipe
# =========================================================

class SwipeIn(BaseModel):
    post_id: int = Field(gt=0, description="ID des bewerteten Posts")
    direction: bool  # True = Rechts/Cool, False = Links/Nicht cool


class SwipeSession(BaseModel):
    # voter_id kommt aus dem Login-Token (Server), NICHT vom Client.
    # created_at setzt die DB selbst.
    target_user_id: int
    swipes: List[SwipeIn] = Field(min_length=1, max_length=20)


class SwipeSessionOut(BaseModel):
    success: bool
    total_points: int = Field(ge=0)
    breakdown: dict[str, int] = Field(
        description="Punkte aufgeschlüsselt nach Flag: {None: X, engagement: Y, creativity: Z, productivity: W}"
    )
    message: Optional[str] = None
    # XP, die der BEWERTER fuer diese Session bekommt (Grund-XP + evtl. Streak-Bonus)
    xp_gained: int = Field(ge=0)
    # Neuer Streak-Stand des Bewerters nach dieser Session (fuer die "🔥 x Tage"-UI)
    streak: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class LeaderboardEntry(BaseModel):
    target_user_id: int
    username: str
    profile_picture_url: Optional[str] = None
    total_points: int
    total_ratings: int

    model_config = ConfigDict(from_attributes=True)



class LeaderboardMe(BaseModel):
    """Die 'Du'-Zeile: der eigene Stand, auch wenn man nicht in den Top 7 ist."""
    my_rank: int                 # eigener Platz heute (1 = Tagessieger)
    my_points: int               # heute erhaltene Punkte
    points_to_next: int          # Punkte-Abstand zum naechsthoeheren (0 = schon #1)


class LeaderboardOut(BaseModel):
    """Kompletter Leaderboard-Response: Top-Liste + eigene Zeile in einem Request."""
    entries: List[LeaderboardEntry]
    me: LeaderboardMe

class ActivityOut(BaseModel):
    type: str
    payload: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)



class MyTargetOut(BaseModel):
    user_data: UserOut
    posts: List[Post]


# =========================================================
# Chat / WebSocket Schemas
# =========================================================
# Alle Chat-Nachrichten laufen über EINEN WebSocket-Endpoint.
# Damit Server + Client wissen worum's bei jeder einzelnen Nachricht geht,
# trägt jede Nachricht ein "kind"-Feld:
#   "dm"      → Direct Message  (1:1)
#   "group"   → Gruppen-Nachricht
#   "ack"     → Bestätigung vom Server an den Sender ("ist raus")
#   "error"   → wenn was schief lief (z.B. Validierung)
#
# Literal["dm"] sorgt dafür dass Pydantic die Nachricht nur akzeptiert
# wenn EXAKT "dm" drinsteht — wer "DM" oder "direct" schickt wird abgewiesen.


class ChatMessageIn(BaseModel):
    """DM vom Client. JSON: { "kind": "dm", "to": <user_id>, "message": "..." }"""
    kind: Literal["dm"]
    to: int  # recipient user_id
    message: str = Field(min_length=1, max_length=4096) #ab jetzt verschlüsselt
    # Vom Client erzeugte UUID pro Nachricht. Der Client schickt sie mit und
    # erkennt damit seine eigene, später gesyncte Server-Zeile wieder (Dedup).
    client_msg_id: Optional[str] = None

    # extra="forbid" → unbekannte Felder werden abgewiesen statt ignoriert
    # (kleine zusätzliche Härtung gegen versehentliche oder bösartige Payloads)
    model_config = ConfigDict(extra="forbid")


class GroupChatMessageIn(BaseModel):
    """Group-Message vom Client. JSON: { "kind": "group", "to": <group_chat_id>, "message": "...", "key_version": <int> }"""
    kind: Literal["group"]
    to: int  # group_chat_id
    message: str = Field(min_length=1, max_length=4096)
    # Mit welcher Schlüssel-Epoche der Client die message verschlüsselt hat.
    # Der Server prüft das gegen die aktuelle Epoche der Gruppe (siehe _prepare_group_send).
    key_version: int
    client_msg_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ChatMessageOut(BaseModel):
    """Eingehende DM die der Empfänger über seinen Socket bekommt."""
    kind: Literal["dm"] = "dm"
    sender_id: int
    message: str
    created_at: datetime
    client_msg_id: Optional[str] = None

class MessageOut(BaseModel):
    id: int
    sender_id: int
    recipient_id: int
    message: str
    created_at: datetime
    client_msg_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)



class GroupChatMessageOut(BaseModel):
    """Eingehende Gruppen-Nachricht die ein Gruppen-Mitglied bekommt."""
    kind: Literal["group"] = "group"
    group_chat_id: int
    sender_id: int
    message: str
    created_at: datetime
    # Mit welcher Epoche die message verschlüsselt ist -> Empfänger weiß, welchen
    # Schlüssel er zum Entschlüsseln nehmen muss.
    key_version: int
    client_msg_id: Optional[str] = None

class GroupMessageOut(BaseModel):
    id: int
    group_chat_id: int
    sender_id: int
    message: str
    created_at: datetime
    # Optional, weil Altbestand aus der Klartext-Zeit keine Version hat.
    key_version: Optional[int] = None
    client_msg_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)



class ChatAck(BaseModel):
    """Bestätigung an den Sender nach jedem Send.
    delivered_live = Anzahl Empfänger die's live bekommen haben.
      - DM:     0 (Empfänger offline, gequeued) oder 1 (live ausgeliefert)
      - Group:  0..n (Anzahl online Mitglieder die's bekommen haben)
    Der Rest landet in der pending Queue und wird beim Reconnect ausgeliefert."""
    kind: Literal["ack"] = "ack"
    to: int  # user_id (bei DM) oder group_chat_id (bei group)
    delivered_live: int


class GroupChatUpdate(BaseModel):
    group_name: Optional[str] = None
    profile_picture: Optional[str] = None


class GroupChatInformationOut(BaseModel):
    group_chat_id: int
    group_name: Optional[str] = None
    profile_picture: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class GroupMemberOut(BaseModel):
    """Ein Mitglied einer Gruppe. user_id braucht das Frontend fürs Rekey
    (Schlüssel pro Mitglied verschlüsseln), Name/Bild für die UI-Mitgliederliste."""
    user_id: int
    username: str
    profile_picture_url: Optional[str] = None

class GroupChatJoinCodeOut(BaseModel):

    code: int
    created_at: datetime
    group_chat_id: int

class GroupChatJoinOut(BaseModel):

    message: str
    group_chat_id: int



# E2EE


class PublicKeyUpload(BaseModel):
    """Was der Client hochlädt: nur seinen öffentlichen Schlüssel (base64)."""
    public_key: str = Field(min_length=1, max_length=100)


class PublicKeyOut(BaseModel):
    """Was wir zurückgeben, wenn jemand einen Schlüssel abfragt."""
    user_id: int
    public_key: str

    model_config = ConfigDict(from_attributes=True)


# --- Gruppen-E2EE: Rekey / Schlüssel-Verteilung ---

class GroupKeyCopy(BaseModel):
    """Eine mit dem Public Key eines Mitglieds verschlüsselte Kopie des neuen
    Gruppenschlüssels. Der Server sieht nur diesen Ciphertext, nie den Klartext-Key."""
    recipient_id: int
    encrypted_key: str = Field(min_length=1)


class GroupRekeyUpload(BaseModel):
    """Was der Client beim Rekey hochlädt: für JEDES aktuelle Mitglied eine Kopie
    des neuen Gruppenschlüssels."""
    keys: List[GroupKeyCopy] = Field(min_length=1)


class GroupRekeyOut(BaseModel):
    """Die neu angelegte Epoche. Der Client sendet danach mit dieser key_version."""
    key_version: int


class GroupKeyOut(BaseModel):
    """Eine Schlüssel-Kopie, die ein Mitglied beim Abruf bekommt."""
    group_chat_id: int
    key_version: int
    encrypted_key: str

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Feed-Impressions (Trainingsdaten für Lytir)
# =========================================================
#
# GRUNDREGEL FÜR DIESE ZWEI SCHEMAS: so wenig ablehnen wie irgend möglich.
#
# Der Client hat KEIN Retry. Er feuert den Batch ab, loggt einen Fehlerstatus
# höchstens im Debug-Log und vergisst ihn. Jede strenge Validierung hier ist
# damit keine Absicherung, sondern eine stille Datenverlust-Falle: ein 422 wegen
# eines einzigen krummen Feldes wirft den KOMPLETTEN Batch weg, und niemand
# merkt es.
#
# Deshalb wehren diese Schemas nur echten Unsinn ab (falsche Typen, absurde
# Größenordnungen, Riesen-Payloads). Sie setzen bewusst KEINE Produktregeln
# durch — die Unter-/Obergrenze für dwell_ms und die erlaubten feed_variant-Werte
# kennt der Client, und wenn er sie in einer künftigen Version ändert, soll das
# Backend die Daten trotzdem annehmen statt sie wortlos zu verschlucken.


class ImpressionIn(BaseModel):
    """Ein Post innerhalb eines Flushes.

    Der Client schickt KUMULATIV: derselbe Post taucht bei jedem Flush erneut
    auf, solange seine Uhr läuft — mit gewachsenem dwell_ms und ggf. gekippten
    Booleans. Duplikate sind hier der Normalfall, nicht der Fehlerfall.
    """

    post_id: int = Field(gt=0)
    # 0-basiert, beim ersten Sichtbarwerden festgehalten. Die Obergrenze ist reiner
    # Unsinns-Schutz, keine echte Feed-Grenze (Pagination kann weit zählen).
    position: int = Field(ge=0, le=100_000)
    shown_at: datetime          # Client-Uhr, ISO8601 UTC ("Z"), 3 oder 6 Nachkommastellen
    # Der Client kappt bei 180000 (genau dieser Wert heißt "gekappt", nicht
    # "gemessen") und sendet unter 1000 gar nicht erst. Beides wird hier NICHT
    # erzwungen — siehe Grundregel oben. le = 24h als reine Absurditätsgrenze.
    dwell_ms: int = Field(ge=0, le=86_400_000)

    # Vier getrennte Signale. reported ist NEGATIV, die anderen drei positiv —
    # nicht zu einem "reacted" zusammenfalten, sonst lernt das Modell, gemeldete
    # Posts häufiger auszuspielen.
    voted: bool
    opened_comments: bool
    shared: bool
    reported: bool


class ImpressionBatch(BaseModel):
    """Was der Client pro Flush hochlädt (10s-Timer oder Gate-Trigger).

    user_id kommt aus dem Token, nie aus dem Body.
    """

    # Client-UUID pro Feed-Sitzung. Absichtlich str und nicht UUID: ein
    # Formatfehler soll den Batch nicht kippen. Zusammen mit user_id und post_id
    # ist das der Konflikt-Key des Upserts.
    feed_session_id: str = Field(min_length=1, max_length=64)
    # Aktuell "local" | "for_you". Bewusst kein Literal: ein neuer Feed-Typ im
    # Frontend würde sonst jeden Batch mit 422 verwerfen, ohne dass es auffällt.
    # Unbekannte Werte landen in der DB und werden im Router geloggt.
    feed_variant: str = Field(min_length=1, max_length=32)
    # Nur zur Schiefstands-Diagnose (Client-Uhr vs. Server-Uhr). Wird bewusst
    # NICHT gespeichert: es ist ein Wert pro Batch, keiner pro Zeile.
    client_sent_at: datetime
    # Kein fachliches Limit — der Client kennt keins und schickt realistisch
    # einstellige bis wenige dutzend Items. max_length ist nur ein Schutz davor,
    # dass jemand 100k Zeilen in einem Request abliefert.
    items: List[ImpressionIn] = Field(min_length=1, max_length=500)
