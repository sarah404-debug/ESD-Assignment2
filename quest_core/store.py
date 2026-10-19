"""Quest storage: one SQLite database file shared by every service.

SQLite keeps the whole database in a single file. In Docker that file sits on
a named volume mounted into both the HTTP and the gRPC container, so both read
and write the same quests. Quests also survive a restart now (before, they
lived in the HTTP app's memory and were lost).

The file location comes from the QUEST_DB_PATH environment variable
(default: data/quests.db, relative to where the service is started).

Every function opens its own short connection, so it is safe to call from
many threads (FastAPI runs requests in a thread pool) and from several
processes at once. SQLite's locking makes each write all-or-nothing.
"""
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("QUEST_DB_PATH", "data/quests.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quests (
    instance_id TEXT PRIMARY KEY,
    quest_id    INTEGER NOT NULL,
    category    TEXT NOT NULL,
    difficulty  TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL      -- Unix timestamp (seconds)
);
-- Which quest each Idempotency-Key created (see POST /quests).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES quests(instance_id),
    created_at  REAL NOT NULL
);
"""

_COLUMNS = ("instance_id", "quest_id", "category", "difficulty", "text", "status", "created_at")
_schema_ready = False


def _connect():
    global _schema_ready
    if not _schema_ready:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we start transactions ourselves (see _transaction).
    # timeout: if another process is writing, wait up to 5 s instead of failing.
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if not _schema_ready:
        # WAL mode lets readers keep reading while someone writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _schema_ready = True
    return conn


@contextmanager
def _transaction():
    """Run several statements as one all-or-nothing write.

    BEGIN IMMEDIATE takes the write lock straight away, so two requests (even
    from different processes) can't both check-then-write at the same time.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _row_to_quest(row):
    return dict(row) if row is not None else None


def get_quest(instance_id):
    """Return one quest as a dict, or None if there is no such quest."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM quests WHERE instance_id = ?", (instance_id,)).fetchone()
        return _row_to_quest(row)
    finally:
        conn.close()


def list_quests(status=None):
    """Return all quests (oldest first), optionally only those with a given status."""
    conn = _connect()
    try:
        if status is None:
            rows = conn.execute("SELECT * FROM quests ORDER BY created_at").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quests WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        return [_row_to_quest(r) for r in rows]
    finally:
        conn.close()


def insert_quest(quest, idempotency_key=None):
    """Save a new quest. Returns (stored_quest, created).

    With an idempotency_key, the key and the quest are saved in one
    transaction. If the key was used before, nothing is saved and the quest
    that key created earlier is returned with created=False.
    """
    with _transaction() as conn:
        if idempotency_key is not None:
            row = conn.execute(
                "SELECT q.* FROM idempotency_keys k JOIN quests q ON q.instance_id = k.instance_id"
                " WHERE k.key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return _row_to_quest(row), False

        conn.execute(
            f"INSERT INTO quests ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
            tuple(quest[c] for c in _COLUMNS),
        )
        if idempotency_key is not None:
            conn.execute(
                "INSERT INTO idempotency_keys (key, instance_id, created_at) VALUES (?, ?, ?)",
                (idempotency_key, quest["instance_id"], time.time()),
            )
    return quest, True


def change_status(instance_id, from_status, to_status):
    """Move a quest from one status to another, only if it still has from_status.

    Returns True if the quest was changed. The check and the change happen in
    one SQL statement, so if two requests try to resolve the same quest at the
    same moment, only one of them succeeds.
    """
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE quests SET status = ? WHERE instance_id = ? AND status = ?",
            (to_status, instance_id, from_status),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()
