"""Tests for authentication & data isolation (§2).

Covers: missing token, malformed token, unknown key, cross-tenant hiding,
tenant_id exclusion from responses, public endpoints skip auth.
"""

from tests.conftest import AUTH_A, AUTH_B


class TestAuthFailures:
    def test_missing_token(self, client):
        r = client.get("/v1/patients", params={"phone": "0912345678"})
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "BAD_KEY"
        assert "www-authenticate" in r.headers

    def test_malformed_token_no_prefix(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers={"Authorization": "Bearer bad_token"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "BAD_KEY"

    def test_unknown_key(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers={"Authorization": "Bearer sk_unknown_key"},
        )
        assert r.status_code == 401

    def test_empty_bearer(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers={"Authorization": "Bearer "},
        )
        assert r.status_code == 401


class TestPublicEndpoints:
    def test_docs_no_auth(self, client):
        r = client.get("/docs")
        assert r.status_code == 200

    def test_openapi_json_no_auth(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200


class TestDataIsolation:
    """§2.2 — a caller cannot see another caller's per-tenant data."""

    def test_tenant_a_cannot_see_tenant_b_patients(self, client):
        r_a = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers=AUTH_A,
        )
        r_b = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers=AUTH_B,
        )
        ids_a = {p["patient_id"] for p in r_a.json()["data"]}
        ids_b = {p["patient_id"] for p in r_b.json()["data"]}
        shared = ids_a & ids_b
        for pid in shared:
            assert pid.startswith("pt_"), "only canonical patients should overlap"

    def test_tenant_id_excluded_from_response(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers=AUTH_A,
        )
        for p in r.json()["data"]:
            assert "tenant_id" not in p

    def test_canonical_fixtures_visible_to_all(self, client):
        r_a = client.get("/v1/appointments/apt_00417", headers=AUTH_A)
        r_b = client.get("/v1/appointments/apt_00417", headers=AUTH_B)
        assert r_a.status_code == 200
        assert r_b.status_code == 200


class TestRequestId:
    def test_echoes_client_request_id(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers={**AUTH_A, "X-Request-Id": "my-req-123"},
        )
        assert r.headers.get("x-request-id") == "my-req-123"

    def test_generates_request_id_when_absent(self, client):
        r = client.get(
            "/v1/patients",
            params={"phone": "0912345678"},
            headers=AUTH_A,
        )
        assert r.headers.get("x-request-id", "").startswith("req_")
