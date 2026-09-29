"""Tests for Appointment CRUD: POST /v1/appointments, GET ./{id}, GET ...?date= (§3.2)."""

from tests.conftest import AUTH_A, AUTH_B, write_headers


def _tenant_a_patient(client):
    r = client.get("/v1/patients", params={"phone": "0912345678"}, headers=AUTH_A)
    return r.json()["data"][0]["patient_id"]


def _tenant_a_slot(client):
    r = client.get(
        "/v1/slots",
        params={
            "clinic_id": "c_001",
            "from": "2026-09-15T00:00:00Z",
            "to": "2026-09-15T23:59:59Z",
        },
        headers=AUTH_A,
    )
    return r.json()["data"][0]["slot_id"]


class TestCreateAppointment:
    """POST /v1/appointments — §1.1.10 inbound booking."""

    def test_create_success(self, client):
        pid = _tenant_a_patient(client)
        sid = _tenant_a_slot(client)
        r = client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "BOOKED"
        assert body["version"] == 1
        assert body["attempt_count"] == 0
        assert "appointment_id" in body
        assert "patient" in body
        assert body["patient"]["patient_id"] == pid

    def test_create_consumes_slot(self, client):
        pid = _tenant_a_patient(client)
        sid = _tenant_a_slot(client)
        client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        r2 = client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        assert r2.status_code == 404

    def test_create_unknown_patient(self, client):
        sid = _tenant_a_slot(client)
        r = client.post(
            "/v1/appointments",
            json={"patient_id": "pt_nonexistent", "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        assert r.status_code == 404

    def test_create_unknown_slot(self, client):
        pid = _tenant_a_patient(client)
        r = client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": "slot_nonexistent"},
            headers={**AUTH_A, **write_headers()},
        )
        assert r.status_code == 404

    def test_create_missing_body_fields(self, client):
        r = client.post(
            "/v1/appointments",
            json={},
            headers={**AUTH_A, **write_headers()},
        )
        assert r.status_code == 422 or r.status_code == 400

    def test_create_no_auth(self, client):
        r = client.post(
            "/v1/appointments",
            json={"patient_id": "pt_3391", "slot_id": "slot_91d2"},
        )
        assert r.status_code == 401

    def test_tenant_id_excluded_from_response(self, client):
        pid = _tenant_a_patient(client)
        sid = _tenant_a_slot(client)
        r = client.post(
            "/v1/appointments",
            json={"patient_id": pid, "slot_id": sid},
            headers={**AUTH_A, **write_headers()},
        )
        assert "tenant_id" not in r.json()

    def test_cross_tenant_patient_not_visible(self, client):
        pid_a = _tenant_a_patient(client)
        r_b_slots = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-15T00:00:00Z",
                "to": "2026-09-15T23:59:59Z",
            },
            headers=AUTH_B,
        )
        sid_b = r_b_slots.json()["data"][0]["slot_id"]
        r = client.post(
            "/v1/appointments",
            json={"patient_id": pid_a, "slot_id": sid_b},
            headers={**AUTH_B, **write_headers()},
        )
        assert r.status_code == 404


class TestGetAppointment:
    """GET /v1/appointments/{id} — Listing 3 canonical shape."""

    def test_get_canonical(self, client):
        r = client.get("/v1/appointments/apt_00417", headers=AUTH_A)
        assert r.status_code == 200
        body = r.json()
        assert body["appointment_id"] == "apt_00417"
        assert body["status"] == "SCHEDULED"
        assert body["clinic_id"] == "cl_vinmec"
        assert "patient" in body
        assert body["patient"]["patient_id"] == "pt_3391"
        assert body["version"] == 3
        assert body["attempt_count"] == 0
        assert "tenant_id" not in body
        assert "slot_id" not in body

    def test_get_listing3_fields(self, client):
        r = client.get("/v1/appointments/apt_00417", headers=AUTH_A)
        body = r.json()
        required_fields = [
            "appointment_id",
            "status",
            "clinic_id",
            "starts_at",
            "ends_at",
            "department",
            "patient",
            "attempt_count",
            "version",
        ]
        for field in required_fields:
            assert field in body, f"Missing required field: {field}"
        nullable_fields = [
            "cancel_reason",
            "transfer_reason",
            "unreachable_reason",
            "confirmed_at",
            "confirmed_via",
            "new_slot_id",
        ]
        for field in nullable_fields:
            assert field in body, f"Missing nullable field: {field}"

    def test_get_not_found(self, client):
        r = client.get("/v1/appointments/apt_nonexistent", headers=AUTH_A)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_get_no_auth(self, client):
        r = client.get("/v1/appointments/apt_00417")
        assert r.status_code == 401


class TestListAppointments:
    """GET /v1/appointments?date=&clinic_id= — call list."""

    def test_list_by_date_and_clinic(self, client):
        r = client.get(
            "/v1/appointments",
            params={"date": "2026-10-14", "clinic_id": "cl_vinmec"},
            headers=AUTH_A,
        )
        assert r.status_code == 200
        body = r.json()
        assert "data" in body
        assert any(a["appointment_id"] == "apt_00417" for a in body["data"])

    def test_list_ordered_by_starts_at(self, client):
        r = client.get(
            "/v1/appointments",
            params={"date": "2026-10-14", "clinic_id": "cl_vinmec"},
            headers=AUTH_A,
        )
        times = [a["starts_at"] for a in r.json()["data"]]
        assert times == sorted(times)

    def test_list_empty_date(self, client):
        r = client.get(
            "/v1/appointments",
            params={"date": "2020-01-01", "clinic_id": "cl_vinmec"},
            headers=AUTH_A,
        )
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_list_invalid_date_format(self, client):
        r = client.get(
            "/v1/appointments",
            params={"date": "14-10-2026", "clinic_id": "cl_vinmec"},
            headers=AUTH_A,
        )
        assert r.status_code == 422 or r.status_code == 400

    def test_list_missing_params(self, client):
        r = client.get("/v1/appointments", headers=AUTH_A)
        assert r.status_code == 422 or r.status_code == 400

    def test_list_pagination(self, client):
        r = client.get(
            "/v1/appointments",
            params={"date": "2026-10-14", "clinic_id": "cl_vinmec", "limit": 1},
            headers=AUTH_A,
        )
        body = r.json()
        assert "next_cursor" in body
        assert "has_more" in body
