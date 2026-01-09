import sqlite3
import time
import uuid

SLOTS = ["nodes", "boars", "horses", "berries", "hemp", "bears", "nobuild"]

SESSION_TTL_SECONDS = 60 * 60


def _now_ts() -> int:
    return int(time.time())


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_atlas_tables(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atlas_sessions (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                waiting_slot TEXT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atlas_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                status TEXT NOT NULL,
                saved_path TEXT,
                attachment_url TEXT,
                updated_at INTEGER NOT NULL,
                UNIQUE(session_id, slot_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atlas_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                saved_path TEXT NOT NULL,
                attachment_url TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atlas_builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                returncode INTEGER NOT NULL,
                stdout TEXT,
                stderr TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )


def _expire_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM atlas_sessions WHERE id=?", (session_id,))
    conn.execute("DELETE FROM atlas_slots WHERE session_id=?", (session_id,))


def get_session_for_channel(db_path: str, guild_id: int, channel_id: int, user_id: int) -> str | None:
    now_ts = _now_ts()
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, updated_at
            FROM atlas_sessions
            WHERE guild_id=? AND channel_id=? AND user_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (guild_id, channel_id, user_id),
        ).fetchone()
        if not row:
            return None

        session_id, updated_at = row
        if now_ts - int(updated_at) > SESSION_TTL_SECONDS:
            _expire_session(conn, session_id)
            return None

        conn.execute("UPDATE atlas_sessions SET updated_at=? WHERE id=?", (now_ts, session_id))
        return session_id


def get_or_create_session(db_path: str, guild_id: int, channel_id: int, user_id: int) -> str:
    session_id = get_session_for_channel(db_path, guild_id, channel_id, user_id)
    if session_id:
        return session_id

    now_ts = _now_ts()
    session_id = uuid.uuid4().hex
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO atlas_sessions (id, guild_id, channel_id, user_id, waiting_slot, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (session_id, guild_id, channel_id, user_id, now_ts, now_ts),
        )
    return session_id


def set_waiting_slot(db_path: str, session_id: str, slot_key: str | None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE atlas_sessions SET waiting_slot=?, updated_at=? WHERE id=?",
            (slot_key, _now_ts(), session_id),
        )


def get_waiting_slot(db_path: str, session_id: str) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT waiting_slot FROM atlas_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    return row[0] if row and row[0] else None


def mark_slot_ready(db_path: str, session_id: str, slot_key: str, saved_path: str, attachment_url: str) -> None:
    if slot_key not in SLOTS:
        raise ValueError(f"Unknown slot: {slot_key}")

    now_ts = _now_ts()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO atlas_slots (session_id, slot_key, status, saved_path, attachment_url, updated_at)
            VALUES (?, ?, 'ready', ?, ?, ?)
            ON CONFLICT(session_id, slot_key)
            DO UPDATE SET status='ready', saved_path=excluded.saved_path,
                          attachment_url=excluded.attachment_url, updated_at=excluded.updated_at
            """,
            (session_id, slot_key, saved_path, attachment_url, now_ts),
        )
        conn.execute(
            """
            INSERT INTO atlas_uploads (session_id, slot_key, saved_path, attachment_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, slot_key, saved_path, attachment_url, now_ts),
        )
        conn.execute("UPDATE atlas_sessions SET updated_at=? WHERE id=?", (now_ts, session_id))


def get_slot_statuses(db_path: str, session_id: str) -> dict[str, str]:
    statuses = {slot: "missing" for slot in SLOTS}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT slot_key, status FROM atlas_slots WHERE session_id=?",
            (session_id,),
        ).fetchall()
    for slot_key, status in rows:
        statuses[str(slot_key)] = str(status)
    return statuses
