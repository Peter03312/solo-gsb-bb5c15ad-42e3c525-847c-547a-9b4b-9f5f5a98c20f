"""End-to-end API tests plus a concurrency guarantee test."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.service import Store

START = "2026-06-01T20:00:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "max_sessions": 2,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_pool(client, film_id="film-1", max_sessions=3, **overrides):
    body = {"film_id": film_id, "max_sessions": max_sessions}
    body.update(overrides)
    resp = client.post("/pools", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["used_sessions"] == 0
    assert data["remaining_sessions"] == max_sessions
    return data



def verify(client, key, start=START, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


# ----------------------------------------------------------------------
# licenses
# ----------------------------------------------------------------------
def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_license_rejects_inverted_window(client):
    resp = client.post(
        "/licenses",
        json={
            "film_id": "f",
            "venue_id": "v",
            "valid_from": "2026-02-01T00:00:00Z",
            "valid_to": "2026-01-01T00:00:00Z",
            "max_sessions": 1,
        },
    )
    assert resp.status_code == 422


def test_verify_approved_and_quota_decreases(client):
    lic = make_license(client)
    resp = verify(client, "vk-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "approved"
    assert data["license_id"] == lic["id"]
    assert data["remaining_quota"] == 1
    assert data["reject_reason"] is None
    assert data["replay"] is False

    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 1
    assert lic_after["remaining_sessions"] == 1


def test_idempotent_replay_returns_original_result(client):
    make_license(client)
    first = verify(client, "vk-replay").json()
    second = verify(client, "vk-replay").json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["decision"] == first["decision"]
    assert second["remaining_quota"] == first["remaining_quota"]
    # quota consumed only once
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 1


def test_idempotency_key_with_changed_payload_conflicts(client):
    make_license(client)
    assert verify(client, "vk-conflict").status_code == 200
    resp = verify(client, "vk-conflict", start="2026-06-02T20:00:00Z")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["idempotency_key"] == "vk-conflict"


def test_prefers_earliest_expiring_license_with_balance(client):
    late = make_license(client, valid_to="2027-06-01T00:00:00Z", max_sessions=5)
    early = make_license(client, valid_to="2026-09-01T00:00:00Z", max_sessions=1)

    first = verify(client, "p-1").json()
    assert first["decision"] == "approved"
    assert first["license_id"] == early["id"]
    assert first["remaining_quota"] == 0

    # early license exhausted -> only one quota consumed per request,
    # the next request falls back to the later-expiring license
    second = verify(client, "p-2").json()
    assert second["decision"] == "approved"
    assert second["license_id"] == late["id"]
    assert second["remaining_quota"] == 4


def test_quota_exhausted_rejects(client):
    make_license(client, max_sessions=1)
    assert verify(client, "q-1").json()["decision"] == "approved"
    resp = verify(client, "q-2")
    data = resp.json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "QUOTA_EXHAUSTED"
    assert data["license_id"] is None


def test_no_license_rejects(client):
    data = verify(client, "n-1", film="unknown", venue="nowhere").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "NO_LICENSE"


def test_time_out_of_window_rejects(client):
    make_license(client, valid_from="2026-01-01T00:00:00Z",
                  valid_to="2026-02-01T00:00:00Z")
    data = verify(client, "t-1", start="2026-06-01T20:00:00Z").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "TIME_OUT_OF_WINDOW"


def test_invalid_time_string_is_422(client):
    resp = client.post(
        "/verifications",
        json={
            "film_id": "film-1",
            "venue_id": "hall-a",
            "start_time": "not-a-time",
            "idempotency_key": "bad-1",
        },
    )
    assert resp.status_code == 422


def test_revocation_blocks_future_but_keeps_history(client):
    lic = make_license(client, max_sessions=5)
    approved = verify(client, "r-1").json()
    assert approved["decision"] == "approved"

    revoke = client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "版权方撤片"})
    assert revoke.status_code == 200
    assert revoke.json()["reason"] == "版权方撤片"

    # future verifications rejected
    data = verify(client, "r-2").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "LICENSE_REVOKED"

    # already-approved session remains traceable
    history = client.get("/verifications/r-1").json()
    assert history["decision"] == "approved"
    assert history["license_id"] == lic["id"]
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["revoked"] is True
    assert lic_after["used_sessions"] == 1

    # revoking twice conflicts
    again = client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "again"})
    assert again.status_code == 409


def test_revoked_license_falls_back_to_other_valid_license(client):
    revoked = make_license(client, valid_to="2026-09-01T00:00:00Z", max_sessions=3)
    backup = make_license(client, valid_to="2027-01-01T00:00:00Z", max_sessions=3)
    client.post(f"/licenses/{revoked['id']}/revoke", json={"reason": "撤片"})
    data = verify(client, "rb-1").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == backup["id"]


def test_list_and_get_verifications(client):
    make_license(client)
    verify(client, "l-1")
    verify(client, "l-2", film="other")  # rejected: NO_LICENSE
    all_rows = client.get("/verifications").json()
    assert len(all_rows) == 2
    approved = client.get("/verifications", params={"decision": "approved"}).json()
    assert len(approved) == 1
    assert approved[0]["idempotency_key"] == "l-1"
    missing = client.get("/verifications/does-not-exist")
    assert missing.status_code == 404


def test_license_not_found(client):
    assert client.get("/licenses/999").status_code == 404
    assert client.post("/licenses/999/revoke", json={"reason": "x"}).status_code == 404


# ----------------------------------------------------------------------
# concurrency: quota must never be exceeded
# ----------------------------------------------------------------------
def test_concurrent_verifications_never_exceed_quota(tmp_path):
    store = Store(str(tmp_path / "concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=3,
    )

    results, errors = [], []

    def worker(i: int) -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-x",
                    venue_id="hall-1",
                    start_time=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
                    idempotency_key=f"ck-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 3
    assert len(rejected) == 17
    assert all(r["reject_reason"] == "QUOTA_EXHAUSTED" for r in rejected)
    assert sorted(r["remaining_quota"] for r in approved) == [0, 1, 2]


# ----------------------------------------------------------------------
# quota pools
# ----------------------------------------------------------------------
def test_create_pool_requires_positive_cap(client):
    resp = client.post("/pools", json={"film_id": "film-1", "max_sessions": 0})
    assert resp.status_code == 422


def test_bind_license_to_pool(client):
    pool = make_pool(client, max_sessions=4)
    lic = make_license(client, max_sessions=10, pool_id=pool["id"])
    assert lic["pool_id"] == pool["id"]
    assert lic["pool"] == {
        "id": pool["id"],
        "film_id": "film-1",
        "name": None,
        "max_sessions": 4,
        "used_sessions": 0,
        "remaining_sessions": 4,
    }


def test_bind_to_missing_pool_is_422(client):
    resp = client.post(
        "/licenses",
        json={
            "film_id": "film-1",
            "venue_id": "hall-a",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
            "max_sessions": 5,
            "pool_id": 999,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "POOL_NOT_FOUND"


def test_bind_pool_of_other_film_is_422(client):
    pool = make_pool(client, film_id="film-other", max_sessions=4)
    resp = client.post(
        "/licenses",
        json={
            "film_id": "film-1",
            "venue_id": "hall-a",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
            "max_sessions": 5,
            "pool_id": pool["id"],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "POOL_FILM_MISMATCH"


def test_approved_verification_consumes_license_and_pool_once(client):
    pool = make_pool(client, max_sessions=3)
    lic = make_license(client, max_sessions=10, pool_id=pool["id"])

    data = verify(client, "pl-1").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] == pool["id"]
    assert data["remaining_quota"] == 9
    assert data["pool_remaining_quota"] == 2

    lic_after = client.get(f"/licenses/{lic['id']}").json()
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert (lic_after["used_sessions"], lic_after["remaining_sessions"]) == (1, 9)
    assert (pool_after["used_sessions"], pool_after["remaining_sessions"]) == (1, 2)
    # 授权内嵌池摘要与池查询口径一致
    assert lic_after["pool"]["used_sessions"] == pool_after["used_sessions"]
    assert lic_after["pool"]["remaining_sessions"] == pool_after["remaining_sessions"]


def test_pool_exhausted_when_own_license_quota_remains(client):
    pool = make_pool(client, max_sessions=1)
    lic = make_license(client, max_sessions=10, pool_id=pool["id"])

    first = verify(client, "pe-1").json()
    assert first["decision"] == "approved"
    assert first["pool_remaining_quota"] == 0

    # 授权自身还剩 9 场，但池已耗尽
    second = verify(client, "pe-2").json()
    assert second["decision"] == "rejected"
    assert second["reject_reason"] == "POOL_EXHAUSTED"
    assert second["license_id"] is None
    assert second["pool_remaining_quota"] is None

    pool_after = client.get(f"/pools/{pool['id']}").json()
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert (pool_after["used_sessions"], pool_after["remaining_sessions"]) == (1, 0)
    assert (lic_after["used_sessions"], lic_after["remaining_sessions"]) == (1, 9)


def test_own_quota_exhausted_takes_precedence_over_pool(client):
    # 唯一授权自身额度先于池耗尽 -> QUOTA_EXHAUSTED（沿用旧原因）
    pool = make_pool(client, max_sessions=10)
    make_license(client, max_sessions=1, pool_id=pool["id"])
    assert verify(client, "oq-1").json()["decision"] == "approved"
    second = verify(client, "oq-2").json()
    assert second["decision"] == "rejected"
    assert second["reject_reason"] == "QUOTA_EXHAUSTED"


def test_pool_replay_does_not_double_consume(client):
    pool = make_pool(client, max_sessions=2)
    make_license(client, max_sessions=10, pool_id=pool["id"])

    first = verify(client, "pr-1").json()
    second = verify(client, "pr-1").json()
    assert second["replay"] is True
    assert second["decision"] == "approved"
    assert second["pool_remaining_quota"] == first["pool_remaining_quota"] == 1
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 1


def test_cross_venue_pool_cap_blocks_after_cap_verifications(client):
    # 同一影片的两个场馆各自绑定同一池：合计核准次数不得超过池上限
    pool = make_pool(client, max_sessions=3)
    make_license(client, venue_id="hall-a", max_sessions=10, pool_id=pool["id"])
    make_license(client, venue_id="hall-b", max_sessions=10, pool_id=pool["id"])

    results = []
    for i, venue in enumerate(["hall-a", "hall-a", "hall-b", "hall-b", "hall-a"]):
        results.append(
            verify(client, f"cv-{i}", venue=venue).json()
        )
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 3
    assert len(rejected) == 2
    assert all(r["reject_reason"] == "POOL_EXHAUSTED" for r in rejected)
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert (pool_after["used_sessions"], pool_after["remaining_sessions"]) == (3, 0)


def test_pool_blocks_bound_but_unbound_license_still_usable(client):
    # hall-a 的授权绑池（池耗尽），hall-b 未绑定 -> 不影响 hall-b
    pool = make_pool(client, max_sessions=1)
    make_license(client, venue_id="hall-a", max_sessions=10, pool_id=pool["id"])
    unbound = make_license(client, venue_id="hall-b", max_sessions=10)

    assert verify(client, "ub-1", venue="hall-a").json()["decision"] == "approved"
    blocked = verify(client, "ub-2", venue="hall-a").json()
    assert blocked["reject_reason"] == "POOL_EXHAUSTED"

    ok = verify(client, "ub-3", venue="hall-b").json()
    assert ok["decision"] == "approved"
    assert ok["license_id"] == unbound["id"]
    assert ok["pool_id"] is None
    assert ok["pool_remaining_quota"] is None


def test_pool_consumption_shared_across_earliest_expiring_licenses(client):
    # 两份绑池授权到期时间不同：早到期授权自身仅 1 场，用完后落到晚到期者；
    # 池余额按同一池共享，两次核准后池耗尽。
    pool = make_pool(client, max_sessions=2)
    early = make_license(
        client, venue_id="hall-a", valid_to="2026-09-01T00:00:00Z",
        max_sessions=1, pool_id=pool["id"],
    )
    late = make_license(
        client, venue_id="hall-a", valid_to="2027-06-01T00:00:00Z",
        max_sessions=10, pool_id=pool["id"],
    )
    first = verify(client, "pp-1").json()
    assert first["license_id"] == early["id"]
    assert first["pool_remaining_quota"] == 1
    second = verify(client, "pp-2").json()
    assert second["license_id"] == late["id"]
    assert second["pool_remaining_quota"] == 0
    third = verify(client, "pp-3").json()
    assert third["reject_reason"] == "POOL_EXHAUSTED"


def test_revocation_does_not_refund_pool_occupancy(client):
    pool = make_pool(client, max_sessions=2)
    lic = make_license(client, max_sessions=10, pool_id=pool["id"])
    verify(client, "rv-p-1")
    assert client.post(
        f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"}
    ).status_code == 200

    # 撤销后：没有可用授权 -> LICENSE_REVOKED，且池占用 1 不退还
    data = verify(client, "rv-p-2").json()
    assert data["reject_reason"] == "LICENSE_REVOKED"
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 1
    assert pool_after["remaining_sessions"] == 1


def test_list_pools_filter_and_license_pool_backref(client):
    p1 = make_pool(client, film_id="film-1", max_sessions=3)
    make_pool(client, film_id="film-2", max_sessions=5)
    make_license(client, film_id="film-1", venue_id="hall-a",
                 max_sessions=2, pool_id=p1["id"])
    pools = client.get("/pools", params={"film_id": "film-1"}).json()
    assert [p["id"] for p in pools] == [p1["id"]]
    lic = client.get("/licenses", params={"film_id": "film-1"}).json()[0]
    assert lic["pool_id"] == p1["id"]
    assert lic["pool"]["id"] == p1["id"]


def test_pool_not_found(client):
    assert client.get("/pools/999").status_code == 404


# ----------------------------------------------------------------------
# concurrency: cross-venue pool cap must never be exceeded
# ----------------------------------------------------------------------
def test_concurrent_verifications_never_exceed_pool_cap(tmp_path):
    store = Store(str(tmp_path / "pool-concurrent.db"))
    pool = store.create_pool(film_id="film-x", max_sessions=5)
    for hall in ("hall-1", "hall-2", "hall-3", "hall-4"):
        store.create_license(
            film_id="film-x",
            venue_id=hall,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
            max_sessions=100,
            pool_id=pool["id"],
        )

    halls = ["hall-1", "hall-2", "hall-3", "hall-4"]
    results, errors = [], []

    def worker(i: int) -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-x",
                    venue_id=halls[i % len(halls)],
                    start_time=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
                    idempotency_key=f"pck-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 5
    assert len(rejected) == 35
    assert all(r["reject_reason"] == "POOL_EXHAUSTED" for r in rejected)
    assert sorted(r["pool_remaining_quota"] for r in approved) == [0, 1, 2, 3, 4]
    pool_after = store.get_pool(pool["id"])
    assert pool_after["used_sessions"] == 5
    assert pool_after["remaining_sessions"] == 0


def test_same_idempotency_key_concurrent_retry_consumes_pool_once(tmp_path):
    store = Store(str(tmp_path / "pool-concurrent-retry.db"))
    pool = store.create_pool(film_id="film-y", max_sessions=3)
    store.create_license(
        film_id="film-y",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=10,
        pool_id=pool["id"],
    )

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-y",
                    venue_id="hall-1",
                    start_time=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
                    idempotency_key="same-key",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert all(r["decision"] == "approved" for r in results)
    assert all(r["pool_remaining_quota"] == 2 for r in results)
    pool_after = store.get_pool(pool["id"])
    assert pool_after["used_sessions"] == 1


# ----------------------------------------------------------------------
# migration: an old-schema database keeps booting and gains pool support
# ----------------------------------------------------------------------
def test_old_database_is_migrated_on_startup(tmp_path):
    db_path = str(tmp_path / "old.db")
    # Build a database with the pre-pool schema and one row of data.
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            film_id TEXT NOT NULL, venue_id TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
            max_sessions INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE revocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id INTEGER NOT NULL UNIQUE REFERENCES licenses (id),
            reason TEXT NOT NULL, revoked_at TEXT NOT NULL
        );
        CREATE TABLE verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            film_id TEXT NOT NULL, venue_id TEXT NOT NULL, start_time TEXT NOT NULL,
            decision TEXT NOT NULL, license_id INTEGER REFERENCES licenses (id),
            reject_reason TEXT, remaining_quota INTEGER, created_at TEXT NOT NULL
        );
        INSERT INTO licenses VALUES (
            1, 'film-1', 'hall-a', '2026-01-01T00:00:00.000000Z',
            '2027-01-01T00:00:00.000000Z', 5, 'old', '2026-01-01T00:00:00.000000Z'
        );
        """
    )
    raw.commit()
    raw.close()

    store = Store(db_path)
    store.ensure()  # must migrate instead of failing

    conn = connect(db_path)
    try:
        lic_cols = {r["name"] for r in conn.execute("PRAGMA table_info(licenses)")}
        ver_cols = {r["name"] for r in conn.execute("PRAGMA table_info(verifications)")}
    finally:
        conn.close()
    assert "pool_id" in lic_cols
    assert {"pool_id", "pool_remaining_quota"} <= ver_cols

    # old license keeps its original semantics
    lic = store.get_license(1)
    assert lic["pool_id"] is None
    assert lic["pool"] is None
    assert lic["max_sessions"] == 5
    app = create_app(db_path)
    with TestClient(app) as c:
        ok = c.post(
            "/verifications",
            json={
                "film_id": "film-1",
                "venue_id": "hall-a",
                "start_time": START,
                "idempotency_key": "mig-1",
            },
        ).json()
        assert ok["decision"] == "approved"
        assert ok["remaining_quota"] == 4
        assert ok["pool_id"] is None

