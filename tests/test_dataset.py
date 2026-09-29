"""The seed dataset (src/clinic_mock/data): what it seeds; a bad one fails loudly."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, time, timedelta

import pytest

from clinic_mock.config import settings
from clinic_mock.dataset import PACKAGED_DATA_DIR, DatasetError, load_dataset, main
from clinic_mock.schemas import Slot
from clinic_mock.seeding import CLINIC_TZ, build_plan, clinic_today
from clinic_mock.store import db, provider_metadata, seed_default
from tests.conftest import AUTH_A, AUTH_B, TENANT_A, write_headers

TODAY = clinic_today()
SUFFIX = f"_{TENANT_A[-4:]}"
NEW_CLINICS = ("c_003", "cl_vinmec_smartcity", "cl_vinmec_centralpark")


def window(first_day: int, days: int) -> dict[str, str]:
    """A GET /v1/slots window on the clinics' clock, counted in days from today."""

    start = datetime.combine(TODAY + timedelta(days=first_day), time(0), CLINIC_TZ)
    return {"from": start.isoformat(), "to": (start + timedelta(days=days)).isoformat()}


def open_slots(client, clinic_id: str, auth=AUTH_A, **span) -> list[dict]:
    params = {"clinic_id": clinic_id, **(span or window(1, 7)), "limit": 100}
    found, cursor = [], None
    while True:
        body = client.get(
            "/v1/slots",
            params={**params, **({"cursor": cursor} if cursor else {})},
            headers=auth,
        ).json()
        found += body["data"]
        cursor = body["next_cursor"]
        if not body["has_more"]:
            return found


# ----- the catalog -----------------------------------------------------------------


def test_every_department_is_staffed_and_every_clinic_can_book():
    dataset = load_dataset()
    departments = {d.name for d in dataset.departments}

    assert (
        len(dataset.clinics) >= 6
        and len(dataset.providers) >= 25
        and len(departments) >= 16
    )
    assert departments == {p.department for p in dataset.providers}
    assert {c.id for c in dataset.clinics} == {p.clinic_id for p in dataset.providers}
    assert all(p.name.startswith("Bác sĩ ") for p in dataset.providers)


def test_every_seeded_row_passes_the_api_schemas():
    """Slots skip validation when seeded (there are thousands): prove they pass."""

    seed_default(profiles={"base", "demo"})

    for slot in db.slots.values():
        Slot.model_validate({**slot.model_dump(), "tenant_id": slot.tenant_id})
    assert provider_metadata("pr_vsc_1")["department"] == "Thần kinh"


def test_the_cli_summarises_a_valid_dataset(capsys):
    assert main(["--profiles", "base,demo"]) == 0
    assert "Vinmec Central Park" in capsys.readouterr().out


# ----- the stock seed --------------------------------------------------------------


def test_the_contract_clinic_keeps_slot_91d2_as_its_only_open_slot(client):
    """The Appendix A reschedule of apt_00417 depends on it being the one offer."""

    slots = client.get("/_harness/slots", headers=AUTH_A).json()

    assert [s["slot_id"] for s in slots if s["clinic_id"] == "cl_vinmec"] == [
        "slot_91d2"
    ]


@pytest.mark.parametrize("clinic_id", [*NEW_CLINICS, "c_001", "c_002"])
def test_every_clinic_has_open_slots_in_the_coming_week(client, clinic_id):
    slots = open_slots(client, clinic_id)

    assert slots
    starts = [datetime.fromisoformat(s["start_time"]) for s in slots]
    assert starts == sorted(starts)  # earliest first, across pages
    assert len({s["slot_id"] for s in slots}) == len(slots)
    if clinic_id in NEW_CLINICS:
        assert all(s["start_time"].endswith("+07:00") for s in slots)
        assert all(7 <= start.hour < 17 for start in starts)


def test_a_seeded_appointment_is_listed_holds_its_slot_and_stays_with_its_key(client):
    # apt_seed_05: Nguyễn Hoàng Nam with pr_301 at c_003, first working day, 10:00.
    appointment_id = f"apt_seed_05{SUFFIX}"
    appointment = client.get(
        f"/v1/appointments/{appointment_id}", headers=AUTH_A
    ).json()
    starts = datetime.fromisoformat(appointment["starts_at"])

    assert (
        appointment["status"],
        appointment["clinic_id"],
        appointment["department"],
    ) == (
        "SCHEDULED",
        "c_003",
        "Nội tổng quát",
    )
    assert appointment["patient"]["verify"] == {
        "full_name": "Nguyễn Hoàng Nam",
        "dob": "1983-03-17",
    }
    assert starts.date() > TODAY and starts.strftime("%H:%M") == "10:00"
    listed = client.get(
        "/v1/appointments",
        params={"date": starts.date().isoformat(), "clinic_id": "c_003"},
        headers=AUTH_A,
    ).json()["data"]
    assert appointment_id in {a["appointment_id"] for a in listed}
    same_time = open_slots(
        client,
        "c_003",
        **{"from": appointment["starts_at"], "to": appointment["ends_at"]},
    )
    assert "pr_301" not in {s["provider_id"] for s in same_time}
    assert (
        client.get(f"/v1/appointments/{appointment_id}", headers=AUTH_B).status_code
        == 404
    )


def test_cancelling_a_seeded_appointment_releases_its_generated_slot(client):
    appointment_id = f"apt_seed_05{SUFFIX}"
    appointment = client.get(
        f"/v1/appointments/{appointment_id}", headers=AUTH_A
    ).json()

    cancelled = client.post(
        f"/v1/appointments/{appointment_id}/cancel",
        json={"cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": True},
        headers={**AUTH_A, **write_headers(1)},
    )

    assert cancelled.status_code == 200
    day = datetime.fromisoformat(appointment["starts_at"]).strftime("%Y%m%d")
    released = open_slots(
        client,
        "c_003",
        **{"from": appointment["starts_at"], "to": appointment["ends_at"]},
    )
    assert f"s_pr_301_{day}_1000{SUFFIX}" in {s["slot_id"] for s in released}


def test_seeded_statuses_look_like_the_lifecycle_left_them(client):
    confirmed = client.get(
        f"/v1/appointments/apt_seed_04{SUFFIX}", headers=AUTH_A
    ).json()
    unreachable = client.get(
        f"/v1/appointments/apt_seed_07{SUFFIX}", headers=AUTH_A
    ).json()
    cancelled = client.get(
        f"/v1/appointments/apt_seed_13{SUFFIX}", headers=AUTH_A
    ).json()

    assert (confirmed["status"], confirmed["confirmed_via"], confirmed["version"]) == (
        "CONFIRMED",
        "sms",
        2,
    )
    assert confirmed["confirmed_at"]
    assert (unreachable["status"], unreachable["attempt_count"]) == ("UNREACHABLE", 1)
    assert cancelled["cancel_reason"] == "NO_LONGER_NEEDED"
    # A cancelled booking leaves its slot open, as POST /cancel does.
    still_open = open_slots(
        client,
        "cl_vinmec_centralpark",
        **{"from": cancelled["starts_at"], "to": cancelled["ends_at"]},
    )
    assert "pr_vcp_3" in {s["provider_id"] for s in still_open}


def test_the_upstream_weekday_block_is_kept_verbatim():
    """Same ids, same UTC times, generated in full for 2026-09-17..2026-10-30."""

    first = db.slots[f"s_20260917_0900{SUFFIX}"]
    legacy = [
        s
        for s in db.slots.values()
        if s.tenant_id == TENANT_A and s.start_time.endswith("Z")
    ]

    assert (first.start_time, first.end_time, first.provider_id, first.clinic_id) == (
        "2026-09-17T09:00:00Z",
        "2026-09-17T09:30:00Z",
        "pr_456",
        "c_001",
    )
    assert len(legacy) == 3 + 10 * 32  # s_987/s_988/s_1024 + ten a weekday


# ----- the demo profile ------------------------------------------------------------


def test_the_demo_profile_is_off_in_the_stock_seed():
    assert "apt_demo_1" not in db.appointments


def test_the_demo_profile_adds_a_shared_call_list_and_contract_clinic_slots(client):
    seed_default(profiles={"demo"})

    appointment = client.get("/v1/appointments/apt_demo_1", headers=AUTH_B).json()
    slots = open_slots(client, "cl_vinmec", auth=AUTH_B)

    assert appointment["patient"]["patient_id"] == "pt_3391"
    assert len({s["department"] for s in slots}) == 6
    assert (
        client.get(
            "/v1/patients", params={"phone": "0912345609"}, headers=AUTH_A
        ).json()["data"][0]["verify"]["full_name"]
        == "Trần Thị Bình"
    )  # the SF-01 namesake of pt_demo_2
    assert "slot_91d2" in db.slots


def test_contract_rows_win_over_a_schedule_on_the_same_day():
    """slot_91d2 (15:00) and apt_00417 (15:30) stay the only bookings at their times."""

    seed_default(profiles={"demo"}, today=date(2026, 10, 1))

    day = {
        s.start_time: s.slot_id
        for s in db.slots.values()
        if s.provider_id == "pr_vinmec_1" and s.start_time.startswith("2026-10-14")
    }
    assert day["2026-10-14T15:00:00+07:00"] == "slot_91d2"
    assert "2026-10-14T15:30:00+07:00" not in day
    assert "2026-10-14T16:00:00+07:00" in day


def test_an_unknown_profile_is_an_error():
    with pytest.raises(DatasetError, match="profile 'dem'"):
        seed_default(profiles={"dem"})


# ----- a bad dataset ---------------------------------------------------------------


@pytest.fixture
def dataset_dir(tmp_path):
    return shutil.copytree(PACKAGED_DATA_DIR, tmp_path / "data")


def edit(directory, name, change) -> None:
    path = directory / f"{name}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    change(rows)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_every_broken_reference_is_reported_at_once(dataset_dir):
    edit(dataset_dir, "providers", lambda rows: rows[0].update(clinic_id="nowhere"))
    edit(dataset_dir, "patients", lambda rows: rows[2].update(phone=rows[1]["phone"]))
    edit(
        dataset_dir,
        "appointments",
        lambda rows: next(
            r for r in rows if r["appointment_id"] == "apt_seed_01"
        ).update(time="12:15"),
    )

    with pytest.raises(DatasetError) as caught:
        load_dataset(dataset_dir)

    problems = "\n".join(caught.value.problems)
    assert "unknown clinic 'nowhere'" in problems
    assert "patient phone" in problems
    assert "apt_seed_01" in problems and "12:15" in problems


def test_a_misspelt_key_is_rejected_not_ignored(dataset_dir):
    edit(dataset_dir, "providers", lambda rows: rows[0].update(departmnet="Tim mạch"))

    with pytest.raises(DatasetError, match="departmnet"):
        load_dataset(dataset_dir)


def test_overlapping_schedules_are_a_double_booking(dataset_dir):
    edit(
        dataset_dir,
        "schedules",
        lambda rows: rows.append(next(r for r in rows if r["provider_id"] == "pr_103")),
    )

    with pytest.raises(DatasetError, match="pr_103 is double-booked"):
        build_plan(load_dataset(dataset_dir), frozenset(), TODAY, 21)


def test_a_horizon_too_short_for_the_call_list_says_so():
    with pytest.raises(DatasetError, match="MOCK_SEED_HORIZON_DAYS"):
        build_plan(load_dataset(), frozenset(), TODAY, 2)


def test_another_dataset_can_be_served_from_a_directory(dataset_dir, monkeypatch):
    edit(
        dataset_dir, "providers", lambda rows: rows[0].update(name="Bác sĩ Thử Nghiệm")
    )
    monkeypatch.setattr(settings.seed, "DATA_DIR", str(dataset_dir))

    seed_default()

    assert provider_metadata("pr_456")["name"] == "Bác sĩ Thử Nghiệm"
    assert db.slots[f"s_987{SUFFIX}"].provider_name == "Bác sĩ Thử Nghiệm"
