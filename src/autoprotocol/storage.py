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
        connection.execute("""CREATE TABLE IF NOT EXISTS review_states (
            meeting_id TEXT NOT NULL REFERENCES meetings(id), source_digest TEXT NOT NULL,
            source_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(meeting_id, source_digest)
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS participants (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, speaker_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, speaker_id),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS segment_edits (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, segment_id TEXT NOT NULL,
            text TEXT NOT NULL, speaker_id TEXT, reviewed INTEGER NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, segment_id),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS revisions (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, revision INTEGER NOT NULL,
            changed_at TEXT NOT NULL, changes_json TEXT NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, revision),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        )""")
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (3)")
        connection.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
            source_digest TEXT NOT NULL, revision INTEGER NOT NULL, input_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','failed')),
            created_at REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, error TEXT, result_json TEXT,
            UNIQUE(meeting_id, source_digest, revision)
        )""")
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (4)")
        connection.execute("""CREATE TABLE IF NOT EXISTS analysis_reviews (
            analysis_id TEXT PRIMARY KEY REFERENCES analyses(id),
            revision INTEGER NOT NULL, edits_json TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS analysis_revisions (
            analysis_id TEXT NOT NULL REFERENCES analyses(id), revision INTEGER NOT NULL,
            changed_at TEXT NOT NULL, changes_json TEXT NOT NULL,
            PRIMARY KEY(analysis_id, revision)
        )""")
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (5)")
        if not connection.execute("SELECT 1 FROM schema_version WHERE version=6").fetchone():
            # Retain old runs and their manual revisions when upgrading the extraction schema.
            connection.execute("ALTER TABLE analyses RENAME TO analyses_legacy")
            connection.execute("""CREATE TABLE analyses (
                id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
                source_digest TEXT NOT NULL, revision INTEGER NOT NULL, input_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','failed')),
                created_at REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                owner TEXT, lease_until REAL, error TEXT, result_json TEXT,
                prompt_version TEXT NOT NULL,
                UNIQUE(meeting_id, source_digest, revision, prompt_version)
            )""")
            connection.execute(
                "INSERT INTO analyses SELECT *, 'meeting-evidence-v2' FROM analyses_legacy"
            )
            for table in ("analysis_reviews", "analysis_revisions"):
                sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                sql = sql.replace(table, table + "_new").replace("analyses_legacy", "analyses")
                connection.execute(sql)
                connection.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
                connection.execute(f"DROP TABLE {table}")
                connection.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            connection.execute("DROP TABLE analyses_legacy")
            connection.execute("INSERT INTO schema_version(version) VALUES (6)")


def check_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.execute("SELECT version FROM schema_version").fetchone()
