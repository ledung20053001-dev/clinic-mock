"""Tests for Discovery endpoints: GET /v1/patients, GET /v1/slots (§3.1)."""

from tests.conftest import AUTH_A


class TestFindPatients:
    """GET /v1/patients?phone=..."""

    def test_find_by_valid_phone(self, client):
        r = client.get("/v1/patients", params={"phone": "0912345678"}, headers=AUTH_A)
        assert r.status_code == 200
        body = r.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        for p in body["data"]:
            assert p["phone"] == "0912345678"
            assert "patient_id" in p
            assert "display_name" in p
            assert "verify" in p
            assert "full_name" in p["verify"]
            assert "dob" in p["verify"]

    def test_find_canonical_patient(self, client):
        r = client.get("/v1/patients", params={"phone": "0912345600"}, headers=AUTH_A)
        assert r.status_code == 200
        data = r.json()["data"]
        assert any(p["patient_id"] == "pt_3391" for p in data)

    def test_phone_not_found_returns_empty(self, client):
        r = client.get("/v1/patients", params={"phone": "0900000000"}, headers=AUTH_A)
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_invalid_phone_format(self, client):
        r = client.get("/v1/patients", params={"phone": "12345"}, headers=AUTH_A)
        assert r.status_code == 422 or r.status_code == 400

    def test_missing_phone_param(self, client):
        r = client.get("/v1/patients", headers=AUTH_A)
        assert r.status_code == 422 or r.status_code == 400

    def test_pagination_fields(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678", "limit": 1},
            headers=AUTH_A,
        )
        body = r.json()
        assert "next_cursor" in body
        assert "has_more" in body

    def test_tenant_id_not_in_response(self, client):
        r = client.get("/v1/patients", params={"phone": "0912345678"}, headers=AUTH_A)
        for p in r.json()["data"]:
            assert "tenant_id" not in p

    def test_no_auth_returns_401(self, client):
        r = client.get("/v1/patients", params={"phone": "0912345678"})
        assert r.status_code == 401


class TestListSlots:
    """GET /v1/slots?clinic_id=&from=&to="""

    def test_list_slots_happy_path(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-15T00:00:00Z",
                "to": "2026-09-15T23:59:59Z",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 200
        body = r.json()
        assert "data" in body
        for s in body["data"]:
            assert s["clinic_id"] == "c_001"
            assert "slot_id" in s
            assert "start_time" in s
            assert "end_time" in s
            assert "provider_id" in s

    def test_canonical_slot_visible(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "cl_vinmec",
                "from": "2026-10-14T00:00:00+07:00",
                "to": "2026-10-14T23:59:59+07:00",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 200
        ids = [s["slot_id"] for s in r.json()["data"]]
        assert "slot_91d2" in ids

    def test_to_must_be_greater_than_from(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-15T12:00:00Z",
                "to": "2026-09-15T06:00:00Z",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_REQUEST"

    def test_window_exceeds_14_days(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-01T00:00:00Z",
                "to": "2026-09-30T00:00:00Z",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 400

    def test_missing_required_params(self, client):
        r = client.get("/v1/slots", headers=AUTH_A)
        assert r.status_code == 422 or r.status_code == 400

    def test_no_slots_returns_empty(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "nonexistent",
                "from": "2026-09-15T00:00:00Z",
                "to": "2026-09-15T23:59:59Z",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_tenant_id_not_in_response(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "c_001",
                "from": "2026-09-15T00:00:00Z",
                "to": "2026-09-15T23:59:59Z",
            },
            headers=AUTH_A,
        )
        for s in r.json()["data"]:
            assert "tenant_id" not in s

    def test_offset_timezone_accepted(self, client):
        r = client.get(
            "/v1/slots",
            params={
                "clinic_id": "cl_vinmec",
                "from": "2026-10-14T00:00:00+07:00",
                "to": "2026-10-14T23:59:59+07:00",
            },
            headers=AUTH_A,
        )
        assert r.status_code == 200
