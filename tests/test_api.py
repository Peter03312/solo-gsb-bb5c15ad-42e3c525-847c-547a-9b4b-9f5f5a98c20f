"""End-to-end API tests plus a concurrency guarantee test."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

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
