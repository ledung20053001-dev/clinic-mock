"""In-memory store with snapshot/restore for test isolation.

Single global `db` instance; snapshot copies are deep via model_dump.

The rows it starts with come from the seed dataset (`dataset.py`, JSON under
`data/`), turned into one day's rows by `seeding.py`. Shared rows, including the
canonical contract fixtures (Listing 3/4 ids: apt_00417, pt_3391, slot_91d2,
slot_77aa, cl_vinmec), are seeded once under the sentinel tenant `t_canonical`
and visible to every caller via `_visible_tenants()`. Tenant rows keep the
existing suffix trick so isolation tests still see distinct rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from functools import lru_cache

from clinic_mock.auth import derive_tenant_id
from clinic_mock.dataset import Dataset, load_dataset
from clinic_mock.schemas import (
    Appointment,
    Patient,
    PatientRef,
    PatientVerify,
    Slot,
)
from clinic_mock.seeding import SeedPlan, build_plan, clinic_today, parse_profiles

# Tenant id under which contract canonical fixtures live. Read by the route
# helpers to widen the tenant filter — see _visible_tenants() in routes.py.
CANONICAL_TENANT = "t_canonical"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    def __init__(self) -> None:
        self.patients: dict[str, Patient] = {}
        self.slots: dict[str, Slot] = {}
        self.appointments: dict[str, Appointment] = {}
        self.snapshots: dict[str, dict] = {}
        self.system_clock_offset_sec: int = 0

    def now(self) -> datetime:
        return datetime.now(UTC).fromtimestamp(
            datetime.now(UTC).timestamp() + self.system_clock_offset_sec,
            tz=UTC,
        )

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def reset(self) -> None:
        self.__init__()

    def snapshot(self) -> str:
        sid = self.new_id("snap")
        self.snapshots[sid] = {
            "patients": {k: v.model_dump() for k, v in self.patients.items()},
            "slots": {k: v.model_dump() for k, v in self.slots.items()},
            "appointments": {k: v.model_dump() for k, v in self.appointments.items()},
            "system_clock_offset_sec": self.system_clock_offset_sec,
        }
        return sid

    def restore(self, sid: str) -> None:
        snap = self.snapshots.get(sid)
        if snap is None:
            from clinic_mock.errors import not_found

            raise not_found(f"snapshot {sid}")
        self.patients = {k: Patient(**v) for k, v in snap["patients"].items()}
        self.slots = {k: Slot(**v) for k, v in snap["slots"].items()}
        self.appointments = {
            k: Appointment(**v) for k, v in snap["appointments"].items()
        }
        self.system_clock_offset_sec = snap["system_clock_offset_sec"]

    def dump(self) -> dict:
        return {
            "patients": [p.model_dump() for p in self.patients.values()],
            "slots": [s.model_dump() for s in self.slots.values()],
            "appointments": [a.model_dump() for a in self.appointments.values()],
        }


db = Store()


# ----- seeding -----

# The dataset of the last seed, for provider lookups between seeds.
_dataset: Dataset | None = None


def provider_metadata(provider_id: str) -> dict[str, str]:
    """Return the stable display metadata associated with a provider id."""

    return (_dataset or load_dataset()).provider(provider_id).metadata()


def _seed_tenants() -> list[str]:
    """Resolve the isolation scopes used by the seed fixtures.

    Parses `MOCK_API_KEYS` directly in env order — the registry is a set
    and would lose insertion order. Falls back to a single derived scope
    if the env is empty so the mock stays usable with zero env tweaks.
    """
    from clinic_mock.config import settings

    raw = settings.mock_auth.API_KEYS
    keys: list[str] = []
    for entry in raw.split(","):
        e = entry.strip()
        if not e:
            continue
        if ":" in e:
            _, e = (p.strip() for p in e.split(":", 1))
        keys.append(e)
    if not keys:
        keys = ["sk_unset"]
    tenants = [derive_tenant_id(k) for k in keys]
    seen: set[str] = set()
    unique: list[str] = []
    for t in tenants:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _scope_suffix(tenant: str) -> str:
    """Short, stable suffix used to disambiguate per-scope fixture copies."""
    return tenant[-4:]


def seed_default(
    *, profiles: Iterable[str] | None = None, today: date | None = None
) -> None:
    """Populate db from the seed dataset (idempotent: reset first).

    Shared rows seed once under `t_canonical` and are visible to every tenant.
    Tenant rows replicate per registered key with a short tenant hash suffix.
    `profiles` and `today` override `MOCK_SEED_PROFILES` and the clinic's date.
    The base profile is always seeded.
    """
    from clinic_mock.config import settings

    global _dataset
    config = settings.seed
    dataset = load_dataset(config.DATA_DIR or None)
    wanted = (
        parse_profiles(config.PROFILES) if profiles is None else frozenset(profiles)
    )
    plan = build_plan(dataset, wanted, today or clinic_today(), config.HORIZON_DAYS)

    db.reset()
    db.system_clock_offset_sec = 0
    _dataset = dataset
    _seed_scope(plan, CANONICAL_TENANT, "shared", "")
    for tenant in _seed_tenants():
        _seed_scope(plan, tenant, "tenant", f"_{_scope_suffix(tenant)}")


def _seed_scope(plan: SeedPlan, tenant: str, scope: str, suffix: str) -> None:
    """Write one scope's rows: shared ones once, tenant ones once per key."""

    for p in plan.patients:
        if p.scope != scope:
            continue
        pid = f"{p.id}{suffix}"
        db.patients[pid] = Patient(
            patient_id=pid,
            tenant_id=tenant,
            display_name=p.display_name,
            phone=p.phone,
            dob=p.dob,
            address=p.address,
            verify=PatientVerify(full_name=p.verify.full_name, dob=p.verify.dob),
        )
    db.slots.update(_slot_rows(plan, tenant, scope, suffix))
    for a in plan.appointments:
        r = a.record
        if r.scope != scope:
            continue
        patient = db.patients[
            f"{r.patient_id}{suffix if a.patient_scope == 'tenant' else ''}"
        ]
        aid = f"{r.appointment_id}{suffix}"
        db.appointments[aid] = Appointment(
            appointment_id=aid,
            tenant_id=tenant,
            slot_id=f"{a.slot_id}{suffix}",
            provider_id=r.provider_id,
            provider_name=a.provider_name,
            status=r.status,
            clinic_id=a.clinic_id,
            starts_at=a.starts_at,
            ends_at=a.ends_at,
            department=a.department,
            patient=PatientRef(
                patient_id=patient.patient_id,
                display_name=patient.display_name,
                verify=patient.verify,
            ),
            cancel_reason=r.cancel_reason,
            transfer_reason=r.transfer_reason,
            unreachable_reason=r.unreachable_reason,
            confirmed_at=now_iso() if r.status == "CONFIRMED" else None,
            confirmed_via=r.confirmed_via,
            attempt_count=r.attempt_count,
            version=r.version,
        )


@lru_cache(maxsize=32)
def _slot_rows(plan: SeedPlan, tenant: str, scope: str, suffix: str) -> dict[str, Slot]:
    """One scope's open slots, built once per plan and key and reused on every reset.

    Thousands of rows per key, rebuilt before every test otherwise. Reusing the
    objects is safe because nothing changes a Slot in place: routes only insert and
    remove them. Built from validated data, so validation is skipped too.
    """

    return {
        f"{s.slot_id}{suffix}": Slot.model_construct(
            slot_id=f"{s.slot_id}{suffix}",
            tenant_id=tenant,
            clinic_id=s.clinic_id,
            start_time=s.start_time,
            end_time=s.end_time,
            provider_id=s.provider_id,
            provider_name=s.provider_name,
            department=s.department,
        )
        for s in plan.slots
        if s.scope == scope
    }
