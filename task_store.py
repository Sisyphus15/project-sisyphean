import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional, Iterable
from datetime import datetime, timezone

logger = logging.getLogger("tasks")

STATUSES = {"PENDING", "IN_PROGRESS", "HOLD", "DONE"}

def utc_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())

@dataclass
class Task:
    id: int
    title: str
    status: str
    assigned_role_id: int
    target_user_id: Optional[int]
    due_at: Optional[int]
    created_by: int
    created_at: int
    updated_at: int
    message_id: Optional[int]
    completed_by: Optional[int]
    completed_at: Optional[int]

class TaskStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.getenv("TASK_DB_PATH", "sisyphus.db")
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              assigned_role_id INTEGER NOT NULL,
              target_user_id INTEGER,
              due_at INTEGER,
              created_by INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              message_id INTEGER,
              completed_by INTEGER,
              completed_at INTEGER
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_role ON tasks(assigned_role_id);")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              actor_user_id INTEGER NOT NULL,
              details TEXT,
              created_at INTEGER NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_log_task_id ON task_log(task_id);")

    def create_task(
        self,
        title: str,
        assigned_role_id: int,
        created_by: int,
        target_user_id: Optional[int] = None,
        due_at: Optional[int] = None,
    ) -> Task:
        now = utc_now()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO tasks (title, status, assigned_role_id, target_user_id, due_at, created_by, created_at, updated_at, message_id)
                VALUES (?, 'PENDING', ?, ?, ?, ?, ?, ?, NULL)
            """, (title, assigned_role_id, target_user_id, due_at, created_by, now, now))
            task_id = cur.lastrowid
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.add_log(task_id, "CREATED", created_by, f"Assigned role_id={assigned_role_id}")
        return self._row_to_task(row)

    def add_log(self, task_id: int, action: str, actor_user_id: int, details: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO task_log (task_id, action, actor_user_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, action, actor_user_id, details, utc_now()),
            )
        logger.info("TASK %s %s by %s | %s", task_id, action, actor_user_id, details or "")

    def complete_task(self, task_id: int, actor_user_id: int) -> None:
        now = utc_now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='DONE', completed_by=?, completed_at=?, updated_at=? WHERE id=?",
                (actor_user_id, now, now, task_id),
            )
        self.add_log(task_id, "COMPLETED", actor_user_id, "Marked DONE via button/command")

    def set_message_id(self, task_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET message_id=?, updated_at=? WHERE id=?",
                         (message_id, utc_now(), task_id))

    def get(self, task_id: int) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def update_status(self, task_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError("Invalid status")
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                         (status, utc_now(), task_id))

    def update_status_by(self, task_id: int, status: str, actor_user_id: int) -> None:
        if status not in STATUSES:
            raise ValueError("Invalid status")
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                         (status, utc_now(), task_id))
        self.add_log(task_id, "STATUS", actor_user_id, f"Set status={status}")

    def assign_role(self, task_id: int, role_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET assigned_role_id=?, updated_at=? WHERE id=?",
                         (role_id, utc_now(), task_id))

    def assign_role_by(self, task_id: int, role_id: int, actor_user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET assigned_role_id=?, updated_at=? WHERE id=?",
                         (role_id, utc_now(), task_id))
        self.add_log(task_id, "ASSIGNED", actor_user_id, f"Assigned role_id={role_id}")

    def list_tasks(
        self,
        status: Optional[str] = None,
        assigned_role_id: Optional[int] = None,
        limit: int = 10
    ) -> list[Task]:
        q = "SELECT * FROM tasks"
        params: list = []
        where = []
        if status:
            where.append("status=?")
            params.append(status)
        if assigned_role_id:
            where.append("assigned_role_id=?")
            params.append(assigned_role_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            assigned_role_id=row["assigned_role_id"],
            target_user_id=row["target_user_id"],
            due_at=row["due_at"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_id=row["message_id"],
            completed_by=row["completed_by"],
            completed_at=row["completed_at"],
        )
