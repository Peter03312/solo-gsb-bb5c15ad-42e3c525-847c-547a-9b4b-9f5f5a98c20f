"""End-to-end tests for film-level shared quota pools."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

START = "2026-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def create_pool(client, film="film-1", cap=10, note=None):
    body = {"film_id": film, "max_sessions": cap}
    if note is not None:
        body["note"] = note
    resp = client.post("/pools", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 100,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


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
# pool registry & binding validation
# ----------------------------------------------------------------------
def test_create_pool_rejects_non_positive_cap(client):
    resp = client.post("/pools", json={"film_id": "film-1", "max_sessions": 0})
    assert resp.status_code == 422


def test_bind_license_to_pool_shows_consistent_usage(client):
    pool = create_pool(client, cap=3)
    lic = make_license(client, pool_id=pool["id"], max_sessions=5)
    assert lic["pool_id"] == pool["id"]
    assert lic["pool"]["id"] == pool["id"]
    assert lic["pool"]["used_sessions"] == 0
    assert lic["pool"]["remaining_sessions"] == 3

    data = verify(client, "k-1").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] == pool["id"]
    assert data["remaining_quota"] == 4
    assert data["pool_remaining_quota"] == 2

    # license view and pool view must agree
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert lic_after["used_sessions"] == 1
    assert lic_after["remaining_sessions"] == 4
    assert pool_after["used_sessions"] == 1
    assert pool_after["remaining_sessions"] == 2
    assert pool_after["bound_licenses"] == 1
    assert lic_after["pool"]["used_sessions"] == pool_after["used_sessions"]
    assert lic_after["pool"]["remaining_sessions"] == pool_after["remaining_sessions"]


def test_bind_missing_pool_is_422(client):
    resp = client.post(
        "/licenses",
        json={
            "film_id": "film-1", "venue_id": "hall-a",
            "valid_from": WINDOW["valid_from"], "valid_to": WINDOW["valid_to"],
            "max_sessions": 5, "pool_id": 999,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "POOL_NOT_FOUND"


def test_bind_pool_of_other_film_is_422(client):
    pool = create_pool(client, film="film-other", cap=3)
    resp = client.post(
        "/licenses",
        json={
            "film_id": "film-1", "venue_id": "hall-a",
            "valid_from": WINDOW["valid_from"], "valid_to": WINDOW["valid_to"],
            "max_sessions": 5, "pool_id": pool["id"],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "POOL_FILM_MISMATCH"


# ----------------------------------------------------------------------
# verification semantics
# ----------------------------------------------------------------------
def test_pool_cap_shared_across_venues_and_exhausted(client):
    pool = create_pool(client, cap=2)
    la = make_license(client, venue_id="hall-a", pool_id=pool["id"], max_sessions=100)
    lb = make_license(client, venue_id="hall-b", pool_id=pool["id"], max_sessions=100)

    r1 = verify(client, "k-a", venue="hall-a").json()
    r2 = verify(client, "k-b", venue="hall-b").json()
    assert r1["decision"] == r2["decision"] == "approved"
    assert {r1["license_id"], r2["license_id"]} == {la["id"], lb["id"]}
    assert r1["pool_remaining_quota"] == 1
    assert r2["pool_remaining_quota"] == 0

    # both licenses still hold their own balance, yet the shared pool is dry
    r3 = verify(client, "k-a2", venue="hall-a").json()
    assert r3["decision"] == "rejected"
    assert r3["reject_reason"] == "POOL_EXHAUSTED"
    assert r3["license_id"] is None
    assert r3["pool_id"] is None
    r4 = verify(client, "k-b2", venue="hall-b").json()
    assert r4["reject_reason"] == "POOL_EXHAUSTED"

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 2
    assert pool_after["remaining_sessions"] == 0


def test_unbound_license_keeps_legacy_semantics_alongside_pool(client):
    pool = create_pool(client, cap=1)
    bound = make_license(client, venue_id="hall-a", max_sessions=1,
                         pool_id=pool["id"],
                         valid_to="2026-09-01T00:00:00Z")
    free = make_license(client, venue_id="hall-a", max_sessions=1,
                        valid_to="2027-06-01T00:00:00Z")

    # earliest-expiring usable license is the pool-bound one
    first = verify(client, "k-1").json()
    assert first["license_id"] == bound["id"]
    assert first["pool_remaining_quota"] == 0

    # pool (and the bound license itself) is exhausted but the later,
    # unbound license still works with legacy semantics
    second = verify(client, "k-2").json()
    assert second["decision"] == "approved"
    assert second["license_id"] == free["id"]
    assert second["pool_id"] is None
    assert second["pool_remaining_quota"] is None

    # every license is now out of its own balance -> classic reason
    last = verify(client, "k-3").json()
    assert last["reject_reason"] == "QUOTA_EXHAUSTED"


def test_license_own_balance_is_the_binding_constraint_when_pool_has_room(client):
    pool = create_pool(client, cap=10)
    lic = make_license(client, pool_id=pool["id"], max_sessions=1)
    data = verify(client, "k-1").json()
    assert data["decision"] == "approved"
    data2 = verify(client, "k-2").json()
    assert data2["reject_reason"] == "QUOTA_EXHAUSTED"

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 1
    assert pool_after["remaining_sessions"] == 9


def test_earliest_expiring_bound_license_consumes_pool(client):
    pool = create_pool(client, cap=5)
    late = make_license(client, pool_id=pool["id"], max_sessions=5,
                        valid_to="2027-06-01T00:00:00Z")
    early = make_license(client, pool_id=pool["id"], max_sessions=5,
                         valid_to="2026-09-01T00:00:00Z")
    data = verify(client, "k-1").json()
    assert data["license_id"] == early["id"]
    assert data["pool_id"] == pool["id"]
    # pool usage is film-global regardless of which bound license was picked
    assert client.get(f"/licenses/{late['id']}").json()["pool"]["used_sessions"] == 1


def test_revocation_does_not_refund_pool(client):
    pool = create_pool(client, cap=2)
    lic = make_license(client, pool_id=pool["id"], max_sessions=5)
    verify(client, "k-1")
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 1
    assert pool_after["remaining_sessions"] == 1

    data = verify(client, "k-2").json()
    assert data["reject_reason"] == "LICENSE_REVOKED"
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1


def test_idempotent_replay_does_not_consume_pool_twice(client):
    pool = create_pool(client, cap=2)
    make_license(client, pool_id=pool["id"], max_sessions=5)
    first = verify(client, "dup").json()
    second = verify(client, "dup").json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["pool_remaining_quota"] == first["pool_remaining_quota"] == 1
    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 1


def test_pool_id_filter_and_verification_filter(client):
    pool = create_pool(client, cap=3)
    bound = make_license(client, pool_id=pool["id"])
    make_license(client, venue_id="hall-c")  # unbound
    verify(client, "k-1")

    bound_licenses = client.get("/licenses", params={"pool_id": pool["id"]}).json()
    assert [l["id"] for l in bound_licenses] == [bound["id"]]

    pool_flow = client.get("/verifications", params={"pool_id": pool["id"]}).json()
    assert len(pool_flow) == 1
    assert pool_flow[0]["idempotency_key"] == "k-1"


# ----------------------------------------------------------------------
# concurrency: pool cap must never be exceeded across venues
# ----------------------------------------------------------------------
def test_concurrent_cross_venue_verifications_never_exceed_pool(tmp_path):
    store = Store(str(tmp_path / "pool-concurrent.db"))
    store.create_pool(film_id="film-x", max_sessions=5, note="cap")
    pool_id = store.list_pools(film_id="film-x")[0]["id"]
    for venue in ("hall-1", "hall-2", "hall-3", "hall-4"):
        store.create_license(
            film_id="film-x",
            venue_id=venue,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
            max_sessions=100,
            pool_id=pool_id,
        )

    results, errors = [], []

    def worker(i: int) -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-x",
                    venue_id=f"hall-{i % 4 + 1}",
                    start_time=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
                    idempotency_key=f"pk-{i}",
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
    pool = store.get_pool(pool_id)
    assert pool["used_sessions"] == 5
    assert pool["remaining_sessions"] == 0


def test_concurrent_same_idempotency_key_single_consumption(tmp_path):
    store = Store(str(tmp_path / "pool-same-key.db"))
    store.create_pool(film_id="film-x", max_sessions=3)
    pool_id = store.list_pools(film_id="film-x")[0]["id"]
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=3,
        pool_id=pool_id,
    )

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-x",
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
    assert len({r["id"] for r in results}) == 1
    assert sum(1 for r in results if not r["replay"]) == 1
    assert all(r["decision"] == "approved" for r in results)
    assert store.get_pool(pool_id)["used_sessions"] == 1
