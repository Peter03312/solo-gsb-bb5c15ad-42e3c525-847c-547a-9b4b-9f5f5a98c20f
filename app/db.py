"""SQLite connection handling and schema definition."""

from __future__ import annotations

import os
import sqlite3

# 仅包含可在"全新库"与"旧库"上都安全执行的建表语句（不引用迁移新增列）。
SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_pools (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id      TEXT    NOT NULL,               -- 池为影片级共享额度，仅该影片可绑定
    name         TEXT,
    max_sessions INTEGER NOT NULL CHECK (max_sessions >= 1),
    note         TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pools_film
    ON quota_pools (film_id);

CREATE TABLE IF NOT EXISTS licenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id       TEXT    NOT NULL,
    venue_id      TEXT    NOT NULL,
    valid_from    TEXT    NOT NULL,               -- canonical UTC ISO-8601
    valid_to      TEXT    NOT NULL,               -- canonical UTC ISO-8601
    max_sessions  INTEGER NOT NULL CHECK (max_sessions >= 1),
    pool_id       INTEGER REFERENCES quota_pools (id),  -- 绑定后不可改；NULL=旧语义
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
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key       TEXT    NOT NULL UNIQUE,
    request_hash          TEXT    NOT NULL,
    film_id               TEXT    NOT NULL,
    venue_id              TEXT    NOT NULL,
    start_time            TEXT    NOT NULL,             -- canonical UTC ISO-8601
    decision              TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected')),
    license_id            INTEGER REFERENCES licenses (id),
    pool_id               INTEGER,                      -- 命中授权绑定的池（批准时各耗一次）
    reject_reason         TEXT,
    remaining_quota       INTEGER,                      -- 消耗后该授权自身剩余
    pool_remaining_quota  INTEGER,                      -- 消耗后所绑池剩余
    created_at            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_license
    ON verifications (license_id, decision);
"""

# 旧库增量迁移：表已存在时 CREATE TABLE IF NOT EXISTS 不会补列；
# 引用新列的索引也必须在补列之后再创建。
_MIGRATIONS = {
    "licenses": [
        ("pool_id", "ALTER TABLE licenses ADD COLUMN pool_id INTEGER REFERENCES quota_pools (id)"),
    ],
    "verifications": [
        ("pool_id", "ALTER TABLE verifications ADD COLUMN pool_id INTEGER"),
        (
            "pool_remaining_quota",
            "ALTER TABLE verifications ADD COLUMN pool_remaining_quota INTEGER",
        ),
    ],
}

_POST_MIGRATION_INDEXES = [
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # quota_pools 必须先于 licenses 的 pool_id 外键迁移就位（SCHEMA 已保证）。
    for table, columns in _MIGRATIONS.items():
        existing = _columns(conn, table)
        for column, ddl in columns:
            if column not in existing:
                conn.execute(ddl)
    for ddl in _POST_MIGRATION_INDEXES:
        conn.execute(ddl)


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()
