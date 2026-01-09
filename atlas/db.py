import sqlite3
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class AtlasSession:
    id: str
    guild_id: int
    user_id: int
    started_at: int
    status: str


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
                user_id INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atlas_images (
                session_id TEXT NOT NULL,
                panel_key TEXT NOT NULL,
                image_path TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )


def create_session(db_path: str, guild_id: int, user_id: int, status: str = "active") -> AtlasSession:
    sid = uuid.uuid4().hex
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO atlas_sessions (id, guild_id, user_id, started_at, status) VALUES (?, ?, ?, ?, ?)",
            (sid, guild_id, user_id, now, status),
        )
    return AtlasSession(id=sid, guild_id=guild_id, user_id=user_id, started_at=now, status=status)


def add_image_record(db_path: str, session_id: str, panel_key: str, image_path: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO atlas_images (session_id, panel_key, image_path, created_at) VALUES (?, ?, ?, ?)",
            (session_id, panel_key, image_path, now),
        )
