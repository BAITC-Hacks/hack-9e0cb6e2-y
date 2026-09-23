CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE meetings (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, occurred_on TEXT, timezone TEXT NOT NULL,
            consent_at REAL NOT NULL, created_at REAL NOT NULL,
            duration_seconds REAL NOT NULL, source_path TEXT NOT NULL,
            audio_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','failed')),
            stage TEXT NOT NULL, error TEXT, result_path TEXT, audio_path TEXT
        );
CREATE TABLE jobs (
            meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
            attempt INTEGER NOT NULL DEFAULT 0, owner TEXT,
            lease_until REAL, heartbeat REAL
        );
CREATE TABLE review_states (
            meeting_id TEXT NOT NULL REFERENCES meetings(id), source_digest TEXT NOT NULL,
            source_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(meeting_id, source_digest)
        );
CREATE TABLE participants (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, speaker_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, speaker_id),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        );
CREATE TABLE segment_edits (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, segment_id TEXT NOT NULL,
            text TEXT NOT NULL, speaker_id TEXT, reviewed INTEGER NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, segment_id),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        );
CREATE TABLE revisions (
            meeting_id TEXT NOT NULL, source_digest TEXT NOT NULL, revision INTEGER NOT NULL,
            changed_at TEXT NOT NULL, changes_json TEXT NOT NULL,
            PRIMARY KEY(meeting_id, source_digest, revision),
            FOREIGN KEY(meeting_id, source_digest) REFERENCES review_states
        );
CREATE TABLE analyses (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id),
            source_digest TEXT NOT NULL, revision INTEGER NOT NULL, input_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','failed')),
            created_at REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, error TEXT, result_json TEXT,
            UNIQUE(meeting_id, source_digest, revision)
        );
CREATE TABLE analysis_reviews (
            analysis_id TEXT PRIMARY KEY REFERENCES analyses(id),
            revision INTEGER NOT NULL, edits_json TEXT NOT NULL
        );
CREATE TABLE analysis_revisions (
            analysis_id TEXT NOT NULL REFERENCES analyses(id), revision INTEGER NOT NULL,
            changed_at TEXT NOT NULL, changes_json TEXT NOT NULL,
            PRIMARY KEY(analysis_id, revision)
        );
INSERT INTO schema_version(version) VALUES (1),(2),(3),(4),(5);
