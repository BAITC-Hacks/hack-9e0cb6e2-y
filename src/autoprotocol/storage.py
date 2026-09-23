import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(database_path: Path):
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")
        connection.execute("""CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, occurred_on TEXT, timezone TEXT NOT NULL,
            consent_at REAL NOT NULL, created_at REAL NOT NULL,
            duration_seconds REAL NOT NULL, source_path TEXT NOT NULL,
            audio_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','failed')),
            stage TEXT NOT NULL, error TEXT, result_path TEXT, audio_path TEXT
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
            attempt INTEGER NOT NULL DEFAULT 0, owner TEXT,
            lease_until REAL, heartbeat REAL
        )""")
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (2)")


def check_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.execute("SELECT version FROM schema_version").fetchone()
