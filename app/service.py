"""Business logic: license registry, quota pools, revocation and idempotent verification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from .db import connect, init_schema
from .timeutil import now_iso, to_iso

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

REASON_NO_LICENSE = "NO_LICENSE"             # 该影片+场馆没有任何授权
REASON_TIME_OUT_OF_WINDOW = "TIME_OUT_OF_WINDOW"  # 有授权但开始时间不在任何有效时段内
REASON_LICENSE_REVOKED = "LICENSE_REVOKED"   # 覆盖该时段的授权均已撤销
REASON_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"   # 授权有效但场次额度已用完
REASON_POOL_EXHAUSTED = "POOL_EXHAUSTED"     # 授权自身有余额，但所绑影片级共享池已耗尽

_LICENSE_SELECT = """
SELECT
    l.id,
    l.film_id,
    l.venue_id,
    l.valid_from,
    l.valid_to,
    l.max_sessions,
    l.pool_id,
    l.note,
    l.created_at,
    p.film_id AS pool_film_id,
    p.max_sessions AS pool_max_sessions,
    p.note AS pool_note,
    p.created_at AS pool_created_at,
    CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS revoked,
    r.reason     AS revoke_reason,
    r.revoked_at AS revoked_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.license_id = l.id AND v.decision = 'approved'
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.pool_id = l.pool_id AND v.decision = 'approved'
    ) AS pool_used_sessions
FROM licenses l
LEFT JOIN revocations r ON r.license_id = l.id
LEFT JOIN quota_pools p ON p.id = l.pool_id
"""

_POOL_SELECT = """
SELECT
    p.id,
    p.film_id,
    p.max_sessions,
    p.note,
    p.created_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.pool_id = p.id AND v.decision = 'approved'
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM licenses l
        WHERE l.pool_id = p.id
    ) AS license_count
FROM quota_pools p
"""


class ConflictError(Exception):
    """409 — e.g. idempotency key reused with a different payload."""

    def __init__(self, message: str, existing: Optional[dict] = None):
        super().__init__(message)
        self.existing = existing


class NotFoundError(Exception):
    """404 — referenced resource does not exist."""


class UnprocessableError(Exception):
    """422 — semantically invalid request that passes schema validation,
    e.g. binding a license to a missing pool or to a pool of another film."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _hash_request(film_id: str, venue_id: str, start_iso: str) -> str:
    payload = json.dumps(
        {"film_id": film_id, "venue_id": venue_id, "start_time": start_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pool_brief(row: sqlite3.Row, used: Optional[int] = None) -> Optional[dict]:
    """Pool snapshot attached to a license row; ``None`` when unbound."""
    if row["pool_id"] is None:
        return None
    if used is None:
        used = row["pool_used_sessions"]
    max_sessions = row["pool_max_sessions"]
    return {
        "id": row["pool_id"],
        "film_id": row["pool_film_id"],
        "max_sessions": max_sessions,
        "used_sessions": used,
        "remaining_sessions": max_sessions - used,
    }


def _license_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    revoked = bool(row["revoked"])
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "pool_id": row["pool_id"],
        "pool": _pool_brief(row),
        "revoked": revoked,
        "revocation": (
            {"reason": row["revoke_reason"], "revoked_at": row["revoked_at"]}
            if revoked
            else None
        ),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _pool_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "bound_licenses": row["license_count"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _verification_out(row: sqlite3.Row, replay: bool) -> dict:
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "start_time": row["start_time"],
        "decision": row["decision"],
        "license_id": row["license_id"],
        "pool_id": row["pool_id"],
        "reject_reason": row["reject_reason"],
        "remaining_quota": row["remaining_quota"],
        "pool_remaining_quota": row["pool_remaining_quota"],
        "verified_at": row["created_at"],
        "replay": replay,
    }


def _decide(candidates: list, start_iso: str):
    """Pick the license to consume, or the reason to reject.

    ``candidates`` is ordered by ``valid_to ASC`` so the first usable entry
    is the earliest-expiring license.  A license is usable only when it
    covers the slot, is not revoked, **and** still has balance of both its
    own quota and its bound pool.  Exactly one unit of the chosen license
    and one unit of its pool are consumed per approval.

    ``POOL_EXHAUSTED`` is reported when in-window, non-revoked licenses
    still have their own balance but every such license is blocked only
    because its bound pool is exhausted; the legacy reasons keep their
    original precedence.
    """
    if not candidates:
        return DECISION_REJECTED, None, None, REASON_NO_LICENSE, None, None
    in_window = [c for c in candidates if c["valid_from"] <= start_iso <= c["valid_to"]]
    if not in_window:
        return DECISION_REJECTED, None, None, REASON_TIME_OUT_OF_WINDOW, None, None
    active = [c for c in in_window if not c["revoked"]]
    if not active:
        return DECISION_REJECTED, None, None, REASON_LICENSE_REVOKED, None, None

    own_balance = [c for c in active if c["used_sessions"] < c["max_sessions"]]
    if not own_balance:
        return DECISION_REJECTED, None, None, REASON_QUOTA_EXHAUSTED, None, None

    # earliest-expiring license with own balance; pool balance re-checked
    # against the pool actually bound to it (pool usage is film-global).
    for cand in own_balance:
        if cand["pool_id"] is None:
            return (
                DECISION_APPROVED, cand["id"], None, None,
                cand["max_sessions"] - cand["used_sessions"] - 1, None,
            )
        pool_max = cand["pool_max_sessions"]
        pool_used = cand["pool_used_sessions"]
        if pool_used < pool_max:
            return (
                DECISION_APPROVED, cand["id"], cand["pool_id"], None,
                cand["max_sessions"] - cand["used_sessions"] - 1,
                pool_max - pool_used - 1,
            )

    # every license that still has its own balance is blocked solely
    # because the pool it is bound to has run dry
    return DECISION_REJECTED, None, None, REASON_POOL_EXHAUSTED, None, None


class Store:
    """Thread-safe facade over the SQLite database.

    A fresh connection is opened per operation; the schema is created
    lazily on first use so importing the module has no side effects.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        """Create the schema if it does not exist yet (idempotent)."""
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            conn = connect(self.db_path)
            try:
                init_schema(conn)
            finally:
                conn.close()
            self._ready = True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure()
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # quota pools (film-level shared session cap across venues)
    # ------------------------------------------------------------------
    def create_pool(
        self,
        *,
        film_id: str,
        max_sessions: int,
        note: Optional[str] = None,
    ) -> dict:
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO quota_pools (film_id, max_sessions, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (film_id, max_sessions, note, now_iso()),
            )
            pool_id = cur.lastrowid
        return self.get_pool(pool_id)

    def get_pool(self, pool_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _POOL_SELECT + " WHERE p.id = ?", (pool_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"quota pool {pool_id} not found")
        return _pool_out(row)

    def list_pools(self, *, film_id: Optional[str] = None) -> list:
        sql, params = _POOL_SELECT, []
        if film_id is not None:
            sql += " WHERE p.film_id = ?"
            params.append(film_id)
        sql += " ORDER BY p.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_pool_out(r) for r in rows]

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    def create_license(
        self,
        *,
        film_id: str,
        venue_id: str,
        valid_from: datetime,
        valid_to: datetime,
        max_sessions: int,
        note: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> dict:
        valid_from_iso, valid_to_iso = to_iso(valid_from), to_iso(valid_to)
        if valid_to_iso <= valid_from_iso:
            raise ValueError("valid_to must be later than valid_from")
        with self._connection() as conn:
            if pool_id is not None:
                pool = conn.execute(
                    "SELECT id, film_id FROM quota_pools WHERE id = ?",
                    (pool_id,),
                ).fetchone()
                if pool is None:
                    raise UnprocessableError(
                        f"quota pool {pool_id} does not exist",
                        code="POOL_NOT_FOUND",
                    )
                if pool["film_id"] != film_id:
                    raise UnprocessableError(
                        f"quota pool {pool_id} belongs to film "
                        f"'{pool['film_id']}', not '{film_id}'",
                        code="POOL_FILM_MISMATCH",
                    )
            cur = conn.execute(
                """
                INSERT INTO licenses
                    (film_id, venue_id, valid_from, valid_to, max_sessions,
                     pool_id, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (film_id, venue_id, valid_from_iso, valid_to_iso,
                 max_sessions, pool_id, note, now_iso()),
            )
            license_id = cur.lastrowid
        return self.get_license(license_id)

    def get_license(self, license_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _LICENSE_SELECT + " WHERE l.id = ?", (license_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"license {license_id} not found")
        return _license_out(row)

    def list_licenses(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> list:
        sql, params = _LICENSE_SELECT, []
        clauses = []
        if film_id is not None:
            clauses.append("l.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("l.venue_id = ?")
            params.append(venue_id)
        if pool_id is not None:
            clauses.append("l.pool_id IS ?")
            params.append(pool_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY l.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_license_out(r) for r in rows]

    def count_licenses(self) -> int:
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]

    def revoke_license(self, license_id: int, reason: str) -> dict:
        """Revoke a license.  Only future verifications are blocked;
        previously approved sessions (and their pool consumption) keep
        their records untouched and are not refunded."""
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                lic = conn.execute(
                    "SELECT id FROM licenses WHERE id = ?", (license_id,)
                ).fetchone()
                if lic is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(f"license {license_id} not found")
                existing = conn.execute(
                    "SELECT * FROM revocations WHERE license_id = ?", (license_id,)
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"license {license_id} is already revoked",
                        existing=dict(existing),
                    )
                revoked_at = now_iso()
                cur = conn.execute(
                    "INSERT INTO revocations (license_id, reason, revoked_at) VALUES (?, ?, ?)",
                    (license_id, reason, revoked_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "license_id": license_id,
                    "reason": reason,
                    "revoked_at": revoked_at,
                }
            except (NotFoundError, ConflictError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ------------------------------------------------------------------
    # verifications
    # ------------------------------------------------------------------
    def verify(
        self,
        *,
        film_id: str,
        venue_id: str,
        start_time: datetime,
        idempotency_key: str,
    ) -> dict:
        """Verify a screening request idempotently.

        The whole read-check-write sequence runs inside a single
        ``BEGIN IMMEDIATE`` transaction, so concurrent requests are
        serialised by SQLite and neither license quotas nor pool caps
        can ever be exceeded.
        """
        start_iso = to_iso(start_time)
        request_hash = _hash_request(film_id, venue_id, start_iso)
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                existing = conn.execute(
                    "SELECT * FROM verifications WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    if existing["request_hash"] != request_hash:
                        raise ConflictError(
                            "idempotency key "
                            f"'{idempotency_key}' was already used with a "
                            "different request payload",
                            existing=_verification_out(existing, replay=True),
                        )
                    return _verification_out(existing, replay=True)

                candidates = conn.execute(
                    _LICENSE_SELECT + """
                    WHERE l.film_id = ? AND l.venue_id = ?
                    ORDER BY l.valid_to ASC, l.id ASC
                    """,
                    (film_id, venue_id),
                ).fetchall()
                (
                    decision, license_id, pool_id, reason,
                    remaining, pool_remaining,
                ) = _decide(candidates, start_iso)

                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO verifications
                        (idempotency_key, request_hash, film_id, venue_id, start_time,
                         decision, license_id, pool_id, reject_reason,
                         remaining_quota, pool_remaining_quota, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, film_id, venue_id, start_iso,
                     decision, license_id, pool_id, reason,
                     remaining, pool_remaining, created_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "idempotency_key": idempotency_key,
                    "film_id": film_id,
                    "venue_id": venue_id,
                    "start_time": start_iso,
                    "decision": decision,
                    "license_id": license_id,
                    "pool_id": pool_id,
                    "reject_reason": reason,
                    "remaining_quota": remaining,
                    "pool_remaining_quota": pool_remaining,
                    "verified_at": created_at,
                    "replay": False,
                }
            except ConflictError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_verification(self, idempotency_key: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM verifications WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"verification '{idempotency_key}' not found")
        return _verification_out(row, replay=False)

    def list_verifications(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        decision: Optional[str] = None,
        license_id: Optional[int] = None,
        pool_id: Optional[int] = None,
        limit: int = 200,
    ) -> list:
        sql = "SELECT * FROM verifications"
        clauses, params = [], []
        if film_id is not None:
            clauses.append("film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("venue_id = ?")
            params.append(venue_id)
        if decision is not None:
            clauses.append("decision = ?")
            params.append(decision)
        if license_id is not None:
            clauses.append("license_id = ?")
            params.append(license_id)
        if pool_id is not None:
            clauses.append("pool_id = ?")
            params.append(pool_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_verification_out(r, replay=False) for r in rows]
