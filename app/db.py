"""SQLite connection handling and schema definition."""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id       TEXT    NOT NULL,
    venue_id      TEXT    NOT NULL,
    valid_from    TEXT    NOT NULL,               -- canonical UTC ISO-8601
    valid_to      TEXT    NOT NULL,               -- canonical UTC ISO-8601
    max_sessions  INTEGER NOT NULL CHECK (max_sessions >= 1),
    note          TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_licenses_film_venue
    ON licenses (film_id, venue_id);

CREATE TABLE IF NOT EXISTS revocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id  INTEGER NOT NULL UNIQUE REFERENCES licenses (id),
    reason      TEXT    NOT NULL,
    revoked_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    request_hash    TEXT    NOT NULL,
    film_id         TEXT    NOT NULL,
    venue_id        TEXT    NOT NULL,
    start_time      TEXT    NOT NULL,             -- canonical UTC ISO-8601
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected')),
    license_id      INTEGER REFERENCES licenses (id),
    reject_reason   TEXT,
    remaining_quota INTEGER,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_license
    ON verifications (license_id, decision);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection tuned for concurrent writers.

    ``isolation_level=None`` puts the driver in autocommit mode so the
    service can control transactions explicitly with ``BEGIN IMMEDIATE``,
    which takes the write lock up-front and serialises concurrent
    verification requests instead of failing with lock-upgrade errors.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()
