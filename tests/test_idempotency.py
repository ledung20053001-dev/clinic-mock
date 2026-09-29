"""Tests for idempotency and versioning (§4.2.3).

Covers: Idempotency-Key replay, IDEMPOTENCY_CONFLICT, If-Match version checks,
and the §4.2.5 rule that Idempotency-Key is evaluated before If-Match on unreachable.
"""

from tests.conftest import AUTH_A, idem_key, write_headers


APPT_ID = "apt_00417"
APPT_VERSION = 3


class TestIdempotencyReplay:
    """Same Idempotency-Key + same payload → cached response."""

    def test_confirm_replay(self, client):
        key = idem_key()
        h = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        r1 = client.post(f"/v1/appointments/{APPT_ID}/confirm", headers=h)
        assert r1.status_code == 200
        r2 = client.post(f"/v1/appointments/{APPT_ID}/confirm", headers=h)
        assert r2.status_code == 200
        assert r1.json() == r2.json()

    def test_cancel_replay(self, client):
        key = idem_key()
        h = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        body = {"cancel_reason": "UNSPECIFIED", "confirmed": True}
        r1 = client.post(f"/v1/appointments/{APPT_ID}/cancel", json=body, headers=h)
        r2 = client.post(f"/v1/appointments/{APPT_ID}/cancel", json=body, headers=h)
        assert r1.json() == r2.json()

    def test_create_appointment_replay_with_header(self, client):
        key = idem_key()
        pid = client.get(
            "/v1/patients", params={"phone": "0912345678"}, headers=AUTH_A
        ).json()["data"][0]["patient_id"]
        sid = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-15T00:00:00Z",
                "to": "2026-09-15T23:59:59Z",
            },
            headers=AUTH_A,
        ).json()["data"][0]["slot_id"]
        h = {**AUTH_A, "Idempotency-Key": key}
        body = {"patient_id": pid, "slot_id": sid}
        r1 = client.post("/v1/appointments", json=body, headers=h)
        assert r1.status_code == 201
        r2 = client.post("/v1/appointments", json=body, headers=h)
        assert r2.status_code == 200 or r2.status_code == 201
        assert "idempotent-replayed" in r2.headers
        assert r2.headers["idempotent-replayed"] == "true"


class TestIdempotencyConflict:
    """Same Idempotency-Key + different payload → 422 IDEMPOTENCY_CONFLICT."""

    def test_confirm_conflict(self, client):
        key = idem_key()
        h1 = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        client.post(f"/v1/appointments/{APPT_ID}/confirm", headers=h1)
        h2 = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION + 1)}
        r = client.post(f"/v1/appointments/{APPT_ID}/confirm", headers=h2)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_cancel_conflict(self, client):
        key = idem_key()
        h = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers=h,
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "COST", "confirmed": True},
            headers=h,
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


class TestUnreachableIdempotency:
    """§4.2.5 — Idempotency-Key is evaluated before If-Match on unreachable."""

    def test_replay_returns_cached_even_with_stale_version(self, client):
        key = idem_key()
        h1 = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        r1 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers=h1,
        )
        assert r1.status_code == 200
        assert r1.json()["attempt_count"] == 1
        h2 = {**AUTH_A, "Idempotency-Key": key, "If-Match": str(APPT_VERSION)}
        r2 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers=h2,
        )
        assert r2.status_code == 200
        assert r2.json()["attempt_count"] == 1

    def test_new_key_increments_attempt_count(self, client):
        key1 = idem_key()
        h1 = {**AUTH_A, "Idempotency-Key": key1, "If-Match": str(APPT_VERSION)}
        r1 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers=h1,
        )
        v2 = r1.json()["version"]
        key2 = idem_key()
        h2 = {**AUTH_A, "Idempotency-Key": key2, "If-Match": str(v2)}
        r2 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "SILENCE"},
            headers=h2,
        )
        assert r2.status_code == 200
        assert r2.json()["attempt_count"] == 2


class TestVersionConflict:
    """If-Match mismatch → 409 VERSION_CONFLICT."""

    def test_stale_version_on_confirm(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(version=1)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_no_if_match_skips_check(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, "Idempotency-Key": idem_key()},
        )
        assert r.status_code == 200
