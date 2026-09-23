from clinic_mock.schemas import Appointment, Patient, PatientRef, PatientVerify
from clinic_mock.store import CANONICAL_TENANT, db
from tests.conftest import AUTH_A


def test_list_patient_appointments_returns_only_owned_records(client) -> None:
    patient = Patient(
        patient_id="p_portal",
        tenant_id=CANONICAL_TENANT,
        display_name="A N.",
        phone="0911111111",
        dob="1990-01-02",
        address="Ha Noi",
        verify=PatientVerify(full_name="Nguyen An", dob="1990-01-02"),
    )
    db.patients[patient.patient_id] = patient
    db.appointments["apt_portal"] = Appointment(
        appointment_id="apt_portal",
        tenant_id=CANONICAL_TENANT,
        slot_id="slot_portal",
        provider_id="pr_301",
        provider_name="Dr Portal",
        status="BOOKED",
        clinic_id="cl_vinmec",
        starts_at="2026-10-01T08:00:00+07:00",
        ends_at="2026-10-01T08:30:00+07:00",
        department="Noi tong quat",
        patient=PatientRef(
            patient_id=patient.patient_id,
            display_name=patient.display_name,
            verify=patient.verify,
        ),
    )

    response = client.get(
        f"/v1/patients/{patient.patient_id}/appointments", headers=AUTH_A
    )

    assert response.status_code == 200
    assert [item["appointment_id"] for item in response.json()["data"]] == ["apt_portal"]


def test_list_patient_appointments_hides_unknown_patient(client) -> None:
    response = client.get("/v1/patients/p_missing/appointments", headers=AUTH_A)

    assert response.status_code == 404


def test_patient_directory_lists_selectable_profiles(client) -> None:
    response = client.get("/v1/patients/directory", headers=AUTH_A)

    assert response.status_code == 200
    records = response.json()["data"]
    assert records
    assert all("patient_id" in patient and "verify" in patient for patient in records)
