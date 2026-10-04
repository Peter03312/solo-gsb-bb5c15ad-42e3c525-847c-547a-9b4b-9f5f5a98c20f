"""SQLite connection handling and schema definition."""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id     TEXT    NOT NULL,                  -- 池为影片级共享额度
    max_sessions INTEGER NOT NULL CHECK (max_sessions >= 1),
    note        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_pools_film ON quota_pools (film_id);

CREATE TABLE IF NOT EXISTS licenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id       TEXT    NOT NULL,
    venue_id      TEXT    NOT NULL,
    valid_from    TEXT    NOT NULL,               -- canonical UTC ISO-8601
    valid_to      TEXT    NOT NULL,               -- canonical UTC ISO-8601
    max_sessions  INTEGER NOT NULL CHECK (max_sessions >= 1),
    pool_id       INTEGER REFERENCES quota_pools (id),  -- 绑定后不可改；NULL=未绑定旧语义
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
    pool_id         INTEGER REFERENCES quota_pools (id),  -- 本次核准所消耗的池
    reject_reason   TEXT,
    remaining_quota INTEGER,                      -- 命中授权消耗后的自身剩余
    pool_remaining_quota INTEGER,                 -- 所绑池消耗后的剩余（无池为 NULL）
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_license
    ON verifications (license_id, decision);
"""

# Columns added after the initial release; applied lazily so databases
# created by older versions keep working without a manual migration.
_EXTRA_COLUMNS = {
    "licenses": [
        ("pool_id", "INTEGER REFERENCES quota_pools (id)"),
    ],
    "verifications": [
        ("pool_id", "INTEGER REFERENCES quota_pools (id)"),
        ("pool_remaining_quota", "INTEGER"),
    ],
}

# Indexes on columns introduced after the first release; created after the
# columns above exist, hence kept out of SCHEMA (IF NOT EXISTS makes this a
# no-op on databases that already have them).
_EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_licenses_pool ON licenses (pool_id)",
    "CREATE INDEX IF NOT EXISTS idx_verifications_pool ON verifications (pool_id, decision)",
]


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


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release when missing."""
    for table, columns in _EXTRA_COLUMNS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    for stmt in _EXTRA_INDEXES:
        conn.execute(stmt)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()
