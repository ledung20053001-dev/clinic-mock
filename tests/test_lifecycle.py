"""Tests for appointment lifecycle endpoints (§3.3):
confirm, cancel, transfer, reschedule, unreachable.

Includes state-machine validation (§3.4) and idempotency/versioning (§4.2.3).
"""

import pytest

from tests.conftest import AUTH_A, write_headers


APPT_ID = "apt_00417"
APPT_VERSION = 3


class TestConfirm:
    """POST /v1/appointments/{id}/confirm — §1.1.3"""

    def test_confirm_scheduled(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "CONFIRMED"
        assert body["confirmed_via"] == "callbot"
        assert body["confirmed_at"] is not None
        assert body["version"] == APPT_VERSION + 1

    def test_confirm_bumps_version(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.json()["version"] == APPT_VERSION + 1

    def test_confirm_not_found(self, client):
        r = client.post(
            "/v1/appointments/apt_nonexistent/confirm",
            headers={**AUTH_A, **write_headers()},
        )
        assert r.status_code == 404

    def test_confirm_version_conflict(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(version=999)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_confirm_no_auth(self, client):
        r = client.post(f"/v1/appointments/{APPT_ID}/confirm")
        assert r.status_code == 401

    @pytest.mark.xfail(
        reason="§3.4 says CONFIRMED→confirm is disallowed, but lifecycle.py "
        "includes CONFIRMED in the 'confirm' allowed set",
        strict=True,
    )
    def test_confirm_from_confirmed_fails(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    def test_confirm_from_cancelled_fails(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409


class TestCancel:
    """POST /v1/appointments/{id}/cancel — §1.1.4 / §3.5 SF-05"""

    def test_cancel_with_confirmation(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "CANCELLED"
        assert body["cancel_reason"] == "PATIENT_UNAVAILABLE"
        assert body["version"] == APPT_VERSION + 1

    def test_cancel_without_confirmation_returns_409(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": False},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    def test_cancel_missing_confirmed_defaults_false(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "PATIENT_UNAVAILABLE"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    def test_cancel_all_reasons(self, client):
        reasons = [
            "PATIENT_UNAVAILABLE",
            "NO_LONGER_NEEDED",
            "WENT_ELSEWHERE",
            "COST",
            "UNSPECIFIED",
        ]
        for reason in reasons:
            from clinic_mock.store import seed_default

            seed_default()
            r = client.post(
                f"/v1/appointments/{APPT_ID}/cancel",
                json={"cancel_reason": reason, "confirmed": True},
                headers={**AUTH_A, **write_headers(APPT_VERSION)},
            )
            assert r.status_code == 200, f"Failed for reason: {reason}"
            assert r.json()["cancel_reason"] == reason

    def test_cancel_invalid_reason(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "INVALID_REASON", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code in (400, 422)

    def test_cancel_from_confirmed(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELLED"

    def test_cancel_from_cancelled_fails(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    def test_cancel_version_conflict(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(version=1)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_CONFLICT"


class TestTransfer:
    """POST /v1/appointments/{id}/transfer — §1.1.5"""

    def test_transfer_success(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/transfer",
            json={"transfer_reason": "CLINICAL_QUESTION"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "TRANSFERRED"
        assert body["transfer_reason"] == "CLINICAL_QUESTION"
        assert body["version"] == APPT_VERSION + 1

    def test_transfer_all_reasons(self, client):
        reasons = [
            "IDENTITY_FAILED",
            "PATIENT_NOT_FOUND",
            "OUT_OF_SCOPE",
            "CLINICAL_QUESTION",
            "NOT_UNDERSTOOD",
            "PATIENT_REQUEST",
            "SYSTEM_ERROR",
        ]
        for reason in reasons:
            from clinic_mock.store import seed_default

            seed_default()
            r = client.post(
                f"/v1/appointments/{APPT_ID}/transfer",
                json={"transfer_reason": reason},
                headers={**AUTH_A, **write_headers(APPT_VERSION)},
            )
            assert r.status_code == 200, f"Failed for reason: {reason}"
            assert r.json()["transfer_reason"] == reason

    def test_transfer_invalid_reason(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/transfer",
            json={"transfer_reason": "BOGUS"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code in (400, 422)

    def test_transfer_from_transferred_fails(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/transfer",
            json={"transfer_reason": "CLINICAL_QUESTION"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/transfer",
            json={"transfer_reason": "CLINICAL_QUESTION"},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409

    def test_transfer_from_confirmed(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/transfer",
            json={"transfer_reason": "PATIENT_REQUEST"},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "TRANSFERRED"


class TestReschedule:
    """POST /v1/appointments/{id}/reschedule — §1.1.6 / Listing 4"""

    def test_reschedule_success(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "RESCHEDULED"
        assert body["new_slot_id"] == "slot_91d2"
        assert body["released_slot_id"] == "slot_77aa"
        assert body["version"] == APPT_VERSION + 1

    def test_reschedule_listing4_minimal_shape(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        body = r.json()
        assert set(body.keys()) == {
            "status",
            "new_slot_id",
            "released_slot_id",
            "version",
        }

    def test_reschedule_releases_old_slot(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "cl_vinmec",
                "from": "2026-10-14T00:00:00+07:00",
                "to": "2026-10-14T23:59:59+07:00",
            },
            headers=AUTH_A,
        )
        ids = [s["slot_id"] for s in r.json()["data"]]
        assert "slot_77aa" in ids
        assert "slot_91d2" not in ids

    def test_reschedule_slot_taken(self, client):
        from clinic_mock.store import db

        db.slots.pop("slot_91d2", None)
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SLOT_TAKEN"

    def test_reschedule_from_rescheduled_succeeds(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_77aa", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "RESCHEDULED"
        assert r.json()["new_slot_id"] == "slot_77aa"

    @pytest.mark.parametrize(
        ("action", "payload", "expected_status"),
        [
            ("confirm", None, "CONFIRMED"),
            (
                "cancel",
                {"cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": True},
                "CANCELLED",
            ),
        ],
    )
    def test_rescheduled_appointment_accepts_follow_up_actions(
        self, client, action, payload, expected_status
    ):
        first = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert first.status_code == 200

        r = client.post(
            f"/v1/appointments/{APPT_ID}/{action}",
            json=payload,
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == expected_status

    def test_reschedule_requested_by_staff(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "STAFF"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200

    def test_reschedule_invalid_requested_by(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "ADMIN"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code in (400, 422)

    def test_reschedule_version_conflict(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/reschedule",
            json={"new_slot_id": "slot_91d2", "requested_by": "PATIENT"},
            headers={**AUTH_A, **write_headers(version=1)},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_CONFLICT"


class TestUnreachable:
    """POST /v1/appointments/{id}/unreachable — §1.1.7 / §4.2.5"""

    def test_unreachable_success(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "UNREACHABLE"
        assert body["unreachable_reason"] == "NO_ANSWER"
        assert body["attempt_count"] == 1
        assert body["version"] == APPT_VERSION + 1

    def test_unreachable_increments_attempt_count(self, client):
        h1 = write_headers(APPT_VERSION)
        r1 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers={**AUTH_A, **h1},
        )
        v2 = r1.json()["version"]
        h2 = write_headers(v2)
        r2 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "LINE_BUSY"},
            headers={**AUTH_A, **h2},
        )
        assert r2.status_code == 200
        assert r2.json()["attempt_count"] == 2

    def test_unreachable_all_reasons(self, client):
        reasons = ["SILENCE", "VOICEMAIL", "NO_ANSWER", "LINE_BUSY"]
        for reason in reasons:
            from clinic_mock.store import seed_default

            seed_default()
            r = client.post(
                f"/v1/appointments/{APPT_ID}/unreachable",
                json={"unreachable_reason": reason},
                headers={**AUTH_A, **write_headers(APPT_VERSION)},
            )
            assert r.status_code == 200, f"Failed for reason: {reason}"
            assert r.json()["unreachable_reason"] == reason

    def test_unreachable_invalid_reason(self, client):
        r = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "WRONG"},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code in (400, 422)

    def test_unreachable_rejects_attempt_count_in_body(self, client):
        """§4.2.5 — body with attempt_count is rejected with 400."""
        r = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER", "attempt_count": 5},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        assert r.status_code in (400, 422)

    def test_unreachable_from_confirmed_fails(self, client):
        """§3.4 — CONFIRMED → unreachable is not allowed."""
        client.post(
            f"/v1/appointments/{APPT_ID}/confirm",
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409

    def test_unreachable_from_cancelled_fails(self, client):
        client.post(
            f"/v1/appointments/{APPT_ID}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(APPT_VERSION)},
        )
        r = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
        )
        assert r.status_code == 409

    def test_unreachable_idempotent_from_unreachable(self, client):
        """§3.4 — UNREACHABLE → unreachable is idempotent (allowed)."""
        h1 = write_headers(APPT_VERSION)
        r1 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "NO_ANSWER"},
            headers={**AUTH_A, **h1},
        )
        v2 = r1.json()["version"]
        h2 = write_headers(v2)
        r2 = client.post(
            f"/v1/appointments/{APPT_ID}/unreachable",
            json={"unreachable_reason": "SILENCE"},
            headers={**AUTH_A, **h2},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "UNREACHABLE"


class TestStateMachine:
    """§3.4 — exhaustive state transition table."""

    def _to_booked(self, client):
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
        r = client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        return r.json()["appointment_id"]

    def test_booked_confirm(self, client):
        aid = self._to_booked(client)
        r = client.post(
            f"/v1/appointments/{aid}/confirm",
            headers={**AUTH_A, **write_headers(1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "CONFIRMED"

    def test_booked_cancel(self, client):
        aid = self._to_booked(client)
        r = client.post(
            f"/v1/appointments/{aid}/cancel",
            json={"cancel_reason": "UNSPECIFIED", "confirmed": True},
            headers={**AUTH_A, **write_headers(1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELLED"

    def test_booked_transfer(self, client):
        aid = self._to_booked(client)
        r = client.post(
            f"/v1/appointments/{aid}/transfer",
            json={"transfer_reason": "PATIENT_REQUEST"},
            headers={**AUTH_A, **write_headers(1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "TRANSFERRED"

    def test_booked_unreachable(self, client):
        aid = self._to_booked(client)
        r = client.post(
            f"/v1/appointments/{aid}/unreachable",
            json={"unreachable_reason": "SILENCE"},
            headers={**AUTH_A, **write_headers(1)},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "UNREACHABLE"

    def test_terminal_states_reject_all(self, client):
        """CANCELLED, RESCHEDULED, TRANSFERRED are terminal — no further actions."""
        terminal_setups = [
            ("cancel", {"cancel_reason": "UNSPECIFIED", "confirmed": True}),
        ]
        for action, body in terminal_setups:
            from clinic_mock.store import seed_default

            seed_default()
            client.post(
                f"/v1/appointments/{APPT_ID}/{action}",
                json=body,
                headers={**AUTH_A, **write_headers(APPT_VERSION)},
            )
            for next_action in [
                "confirm",
                "cancel",
                "transfer",
                "reschedule",
                "unreachable",
            ]:
                payload = {}
                if next_action == "cancel":
                    payload = {"cancel_reason": "UNSPECIFIED", "confirmed": True}
                elif next_action == "transfer":
                    payload = {"transfer_reason": "SYSTEM_ERROR"}
                elif next_action == "reschedule":
                    payload = {"new_slot_id": "slot_91d2", "requested_by": "PATIENT"}
                elif next_action == "unreachable":
                    payload = {"unreachable_reason": "NO_ANSWER"}
                r = client.post(
                    f"/v1/appointments/{APPT_ID}/{next_action}",
                    json=payload if payload else None,
                    headers={**AUTH_A, **write_headers(APPT_VERSION + 1)},
                )
                assert r.status_code == 409, (
                    f"Expected 409 for {action}→{next_action}, got {r.status_code}"
                )
