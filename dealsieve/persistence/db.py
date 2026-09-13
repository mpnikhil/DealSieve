"""SQLite DDL and connection helper for DealSieve persistence (W2).

stdlib sqlite3 only. WAL mode, foreign keys enforced. Nested contract objects are stored as JSON TEXT
(see repo.py for the exact (de)serialization contract); scalar columns exist only for what we filter or
sort on.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id      TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    sender          TEXT,
    subject         TEXT,
    thread_id       TEXT,
    opportunity_id  TEXT NULL REFERENCES opportunities(opportunity_id),
    status          TEXT NOT NULL DEFAULT 'received',
    error           TEXT,
    updated_at      TEXT,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbound_messages_thread_id ON inbound_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_inbound_messages_opportunity_id ON inbound_messages(opportunity_id);

CREATE TABLE IF NOT EXISTS properties (
    property_id         TEXT PRIMARY KEY,
    normalized_address   TEXT UNIQUE,
    apn                  TEXT,
    json                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id       TEXT PRIMARY KEY,
    deal_number          INTEGER UNIQUE,
    property_id          TEXT NOT NULL REFERENCES properties(property_id),
    status               TEXT NOT NULL,
    broker_property_ref  TEXT,
    listing_url          TEXT,
    updated_at           TEXT NOT NULL,
    json                 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opportunities_property_id ON opportunities(property_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opportunities_broker_property_ref ON opportunities(broker_property_ref);
CREATE INDEX IF NOT EXISTS idx_opportunities_listing_url ON opportunities(listing_url);

CREATE TABLE IF NOT EXISTS opportunity_events (
    event_id          TEXT PRIMARY KEY,
    opportunity_id    TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    seq               INTEGER NOT NULL,
    type              TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    source_message_id TEXT,
    json              TEXT NOT NULL,
    UNIQUE(opportunity_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_events_opportunity_id ON opportunity_events(opportunity_id);

CREATE TABLE IF NOT EXISTS source_documents (
    id              TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    message_id      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    text            TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_documents_opportunity_id ON source_documents(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_source_documents_sha256 ON source_documents(sha256);

CREATE TABLE IF NOT EXISTS evidence_observations (
    evidence_id     TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    field           TEXT NOT NULL,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_observations_opportunity_id ON evidence_observations(opportunity_id);

CREATE TABLE IF NOT EXISTS underwriting_runs (
    run_id          TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    policy_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_underwriting_runs_opportunity_id ON underwriting_runs(opportunity_id);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version  TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    raw_yaml        TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skeptic_reports (
    report_id       TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    run_id          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skeptic_reports_opportunity_id ON skeptic_reports(opportunity_id);

CREATE TABLE IF NOT EXISTS outbound_drafts (
    draft_id        TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_drafts_opportunity_id ON outbound_drafts(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_outbound_drafts_status ON outbound_drafts(status);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id  TEXT PRIMARY KEY,
    opportunity_id   TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    kind             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    dedupe_key       TEXT,
    json             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_opportunity_id ON notifications(opportunity_id);

CREATE TABLE IF NOT EXISTS diligence_requests (
    request_id      TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    status          TEXT NOT NULL,
    topic           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    follow_up_count INTEGER NOT NULL DEFAULT 0,
    due_at          TEXT,
    last_follow_up_at TEXT,
    follow_up_reserved_at TEXT,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diligence_requests_opportunity_id ON diligence_requests(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_diligence_requests_status ON diligence_requests(status);

CREATE TABLE IF NOT EXISTS document_analyses (
    analysis_id     TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    message_id      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_analyses_opportunity_id ON document_analyses(opportunity_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add ``column`` to ``table`` if it is missing (for a pre-existing db file created before
    this column was introduced). ``CREATE TABLE IF NOT EXISTS`` alone would not retrofit it."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and foreign keys enabled."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Repo is typically constructed on one thread (e.g. a pytest fixture
    # or an API request handler) but callers such as the Strands agent runtime dispatch synchronous
    # @tool calls onto worker threads. Repo serializes its own access with self._lock, so sharing
    # the connection across threads is safe as long as callers only ever go through Repo's methods.
    conn = sqlite3.connect(str(path), detect_types=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Defensive migration for a db file created before status/error/updated_at/dedupe_key existed.
    _ensure_column(conn, "inbound_messages", "status", "TEXT NOT NULL DEFAULT 'received'")
    _ensure_column(conn, "inbound_messages", "error", "TEXT")
    _ensure_column(conn, "inbound_messages", "updated_at", "TEXT")
    _ensure_column(conn, "notifications", "dedupe_key", "TEXT")
    _ensure_column(conn, "diligence_requests", "follow_up_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "diligence_requests", "due_at", "TEXT")
    _ensure_column(conn, "diligence_requests", "last_follow_up_at", "TEXT")
    _ensure_column(conn, "diligence_requests", "follow_up_reserved_at", "TEXT")
    # These indexes must be created after the column migrations.  Creating them in SCHEMA would
    # make initialization of a pre-Phase-2 database fail before `_ensure_column` can run.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_messages_status ON inbound_messages(status)")
    # Nullable UNIQUE: SQLite treats every NULL as distinct, so legacy notifications without a
    # dedupe key remain valid while any non-null key stays unique.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe_key ON notifications(dedupe_key)")
    # Backfill scalar cadence fields for rows created by the original Phase-2 schema. These
    # columns make follow-up reservation a real SQL compare-and-swap rather than a JSON
    # read/modify/write race.
    conn.execute(
        """
        UPDATE diligence_requests
           SET follow_up_count = COALESCE(json_extract(json, '$.follow_up_count'), 0),
               due_at = json_extract(json, '$.due_at'),
               last_follow_up_at = json_extract(json, '$.last_follow_up_at')
        """
    )
    conn.commit()
