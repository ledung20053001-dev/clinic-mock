"""Tests for patient creation."""

from tests.conftest import AUTH_A, AUTH_B, idem_key


PATIENT_BODY = {
    "first_name": "An",
    "last_name": "L\u00ea V\u0103n",
    "phone": "0356789012",
    "dob": "1990-05-20",
    "address": "Đống Đa, Hà Nội",
}


def test_create_patient_is_visible_in_authenticated_tenant(client):
    created = client.post(
        "/v1/patients",
        json=PATIENT_BODY,
        headers={**AUTH_A, "Idempotency-Key": idem_key()},
    )

    assert created.status_code == 201
    patient = created.json()
    assert patient["patient_id"].startswith("p_")
    assert patient["display_name"] == "An L."
    assert patient["verify"]["full_name"] == "L\u00ea V\u0103n An"
    assert patient["address"] == PATIENT_BODY["address"]

    found = client.get(
        "/v1/patients",
        params={"phone": PATIENT_BODY["phone"]},
        headers=AUTH_A,
    )
    hidden = client.get(
        "/v1/patients",
        params={"phone": PATIENT_BODY["phone"]},
        headers=AUTH_B,
    )
    assert [item["patient_id"] for item in found.json()["data"]] == [
        patient["patient_id"]
    ]
    assert hidden.json()["data"] == []


def test_create_patient_replays_same_idempotency_key(client):
    key = idem_key()
    headers = {**AUTH_A, "Idempotency-Key": key}

    first = client.post("/v1/patients", json=PATIENT_BODY, headers=headers)
    replay = client.post("/v1/patients", json=PATIENT_BODY, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert replay.json()["patient_id"] == first.json()["patient_id"]
    assert replay.headers["Idempotent-Replayed"] == "true"


def test_create_patient_rejects_duplicate_phone(client):
    first = client.post(
        "/v1/patients",
        json=PATIENT_BODY,
        headers={**AUTH_A, "Idempotency-Key": idem_key()},
    )
    duplicate = client.post(
        "/v1/patients",
        json={**PATIENT_BODY, "first_name": "B\u00ecnh"},
        headers={**AUTH_A, "Idempotency-Key": idem_key()},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PATIENT_PHONE_EXISTS"
