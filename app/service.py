"""Business logic: license registry, revocation and idempotent verification."""

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

_LICENSE_SELECT = """
SELECT
    l.id,
    l.film_id,
    l.venue_id,
    l.valid_from,
    l.valid_to,
    l.max_sessions,
    l.note,
    l.created_at,
    CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS revoked,
    r.reason     AS revoke_reason,
    r.revoked_at AS revoked_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.license_id = l.id AND v.decision = 'approved'
    ) AS used_sessions
FROM licenses l
LEFT JOIN revocations r ON r.license_id = l.id
"""


class ConflictError(Exception):
    """409 — e.g. idempotency key reused with a different payload."""

    def __init__(self, message: str, existing: Optional[dict] = None):
        super().__init__(message)
        self.existing = existing


class NotFoundError(Exception):
    """404 — referenced resource does not exist."""


def _hash_request(film_id: str, venue_id: str, start_iso: str) -> str:
    payload = json.dumps(
        {"film_id": film_id, "venue_id": venue_id, "start_time": start_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        "revoked": revoked,
        "revocation": (
            {"reason": row["revoke_reason"], "revoked_at": row["revoked_at"]}
            if revoked
            else None
        ),
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
        "reject_reason": row["reject_reason"],
        "remaining_quota": row["remaining_quota"],
        "verified_at": row["created_at"],
        "replay": replay,
    }


def _decide(candidates: list, start_iso: str):
    """Pick the license to consume, or the reason to reject.

    ``candidates`` is ordered by ``valid_to ASC`` so the first entry with
    remaining quota is the earliest-expiring usable license — exactly one
    quota unit is consumed even when several licenses cover the slot.
    """
    if not candidates:
        return DECISION_REJECTED, None, REASON_NO_LICENSE, None
    in_window = [c for c in candidates if c["valid_from"] <= start_iso <= c["valid_to"]]
    if not in_window:
        return DECISION_REJECTED, None, REASON_TIME_OUT_OF_WINDOW, None
    active = [c for c in in_window if not c["revoked"]]
    if not active:
        return DECISION_REJECTED, None, REASON_LICENSE_REVOKED, None
    for cand in active:
        if cand["used_sessions"] < cand["max_sessions"]:
            remaining_after = cand["max_sessions"] - cand["used_sessions"] - 1
            return DECISION_APPROVED, cand["id"], None, remaining_after
    return DECISION_REJECTED, None, REASON_QUOTA_EXHAUSTED, None


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
    ) -> dict:
        valid_from_iso, valid_to_iso = to_iso(valid_from), to_iso(valid_to)
        if valid_to_iso <= valid_from_iso:
            raise ValueError("valid_to must be later than valid_from")
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO licenses
                    (film_id, venue_id, valid_from, valid_to, max_sessions, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (film_id, venue_id, valid_from_iso, valid_to_iso,
                 max_sessions, note, now_iso()),
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
    ) -> list:
        sql, params = _LICENSE_SELECT, []
        clauses = []
        if film_id is not None:
            clauses.append("l.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("l.venue_id = ?")
            params.append(venue_id)
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
        previously approved sessions keep their records untouched."""
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
        serialised by SQLite and the session quota can never be
        exceeded.
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
                decision, license_id, reason, remaining = _decide(candidates, start_iso)

                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO verifications
                        (idempotency_key, request_hash, film_id, venue_id, start_time,
                         decision, license_id, reject_reason, remaining_quota, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, film_id, venue_id, start_iso,
                     decision, license_id, reason, remaining, created_at),
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
                    "reject_reason": reason,
                    "remaining_quota": remaining,
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
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_verification_out(r, replay=False) for r in rows]
