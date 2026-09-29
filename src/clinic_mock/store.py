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
<<<<<<< HEAD
from clinic_mock.seeding import SeedPlan, build_plan, clinic_today, parse_profiles
=======
>>>>>>> 11e35afaadf3388e8f0748a9ec9ab0132b11652a

# Tenant id under which contract canonical fixtures live. Read by the route
# helpers to widen the tenant filter — see _visible_tenants() in routes.py.
CANONICAL_TENANT = "t_canonical"


# /v1/* path → semantic operation name. Used by the writelog middleware to tag
# each captured request with a stable op label that the scoring harness can
# match against expected SF-detection rules (SF-01 wrong-write, SF-02 slot
# not served, etc.).
WRITELOG_WRITE_OPS: dict[str, str] = {
    "/v1/appointments": "create_appointment",
    "confirm": "confirm",
    "cancel": "cancel",
    "transfer": "transfer",
    "reschedule": "reschedule",
    "unreachable": "unreachable",
}


WRITELOG_READ_OPS: dict[str, str] = {
    "/v1/patients": "find_patients",
    "/v1/slots": "list_slots",
    "/v1/appointments": "list_appointments",
    # /v1/appointments/<id> is handled below
}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 string as produced by `now_iso()` or any RFC 3339 input.

    Accepts both `Z` suffix and explicit `±HH:MM` offsets. Returns None on
    malformed input (so `WriteLog.query` can drop entries without an `at`).
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def derive_writelog_op(method: str, path: str) -> str | None:
    """Map (method, path) to a semantic writelog `op` label.

    Returns None for non-v1 paths (the writelog middleware skips those).
    Returns a `list_*` op for collection GETs (needed for SF-02 scoring —
    the harness needs to see what slot lists the bot was served).
    """
    if not path.startswith("/v1/"):
        return None
    norm = path.rstrip("/")
    if method == "POST" and norm == "/v1/appointments":
        return "create_appointment"
    parts = norm.strip("/").split("/")
    # /v1/appointments/<id>/<action> (writes)
    if (
        method in {"POST"}
        and len(parts) >= 4
        and parts[0] == "v1"
        and parts[1] == "appointments"
        and parts[3] in WRITELOG_WRITE_OPS
    ):
        return parts[3]
    # Single GETs (writes-related reads)
    if (
        method == "GET"
        and len(parts) == 4
        and parts[0] == "v1"
        and parts[1] == "appointments"
    ):
        return "get_appointment"
    # Collection GETs (read paths)
    if method == "GET" and norm in WRITELOG_READ_OPS:
        return WRITELOG_READ_OPS[norm]
    return None


class WriteLog:
    """Append-only log of every /v1/* mutation the mock received.

    Reset by `/_harness/reset`. Read back by `GET /_harness/writelog` for the
    scoring harness (§4.3 step 5). Captures request body + response status
    so the harness can verify writes against expected behaviour.
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, entry: dict) -> None:
        self.entries.append(entry)

    def reset(self) -> None:
        self.entries = []

    def query(
        self,
        op: str | None = None,
        appointment_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        out = self.entries
        if op is not None:
            out = [e for e in out if e.get("op") == op]
        if appointment_id is not None:
            out = [e for e in out if e.get("appointment_id") == appointment_id]
        if since is not None or until is not None:
            since_dt = since
            until_dt = until
            kept: list[dict] = []
            for e in out:
                at_raw = e.get("at")
                if not at_raw:
                    continue
                at_dt = _parse_iso(at_raw)
                if at_dt is None:
                    continue
                if since_dt is not None and at_dt < since_dt:
                    continue
                if until_dt is not None and at_dt > until_dt:
                    continue
                kept.append(e)
            out = kept
        return out


class Store:
    def __init__(self) -> None:
        self.patients: dict[str, Patient] = {}
        self.slots: dict[str, Slot] = {}
        self.appointments: dict[str, Appointment] = {}
        self.snapshots: dict[str, dict] = {}
        self.writelog: WriteLog = WriteLog()
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

    def snapshot(self, tenant_id: str) -> str:
        sid = self.new_id("snap")
        # Snapshots carry `tenant_id` at the envelope level for cross-tenant
        # access checks (see `restore`/`get_snapshot`). Inside `data` we keep
        # the per-row `tenant_id` (and Appointment's `slot_id`/`provider_id`)
        # because Pydantic validation on restore rejects Patient/Slot/Appointment
        # without those fields — `Field(exclude=True)` only affects serialization,
        # not deserialization.
        self.snapshots[sid] = {
            "tenant_id": tenant_id,
            "created_at": now_iso(),
            "data": {
                "patients": {
                    k: {**v.model_dump(), "tenant_id": v.tenant_id}
                    for k, v in self.patients.items()
                },
                "slots": {
                    k: {**v.model_dump(), "tenant_id": v.tenant_id}
                    for k, v in self.slots.items()
                },
                "appointments": {
                    k: {
                        **v.model_dump(),
                        "tenant_id": v.tenant_id,
                        "slot_id": v.slot_id,
                        "provider_id": v.provider_id,
                    }
                    for k, v in self.appointments.items()
                },
                "writelog": list(self.writelog.entries),
                "system_clock_offset_sec": self.system_clock_offset_sec,
            },
        }
        return sid

    def get_snapshot(self, sid: str, caller_tenant_id: str) -> dict | None:
        snap = self.snapshots.get(sid)
        if snap is None or snap["tenant_id"] != caller_tenant_id:
            return None
        data = snap["data"]
        return {
            "snapshot_id": sid,
            "created_at": snap["created_at"],
            "patients": list(data["patients"].values()),
            "slots": list(data["slots"].values()),
            "appointments": list(data["appointments"].values()),
            "writelog": list(data.get("writelog", [])),
            "system_clock_offset_sec": data["system_clock_offset_sec"],
        }

    def list_snapshots(
        self,
        caller_tenant_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        out: list[dict] = []
        for sid, snap in self.snapshots.items():
            if snap["tenant_id"] != caller_tenant_id:
                continue
            if since is not None or until is not None:
                # Compare via datetime objects so cross-format timestamps
                # (now_iso's ms+Z vs caller-supplied µs+offset) compare
                # correctly. Mirrors WriteLog.query's pattern.
                created_at_dt = _parse_iso(snap["created_at"])
                if created_at_dt is None:
                    continue
                if since is not None and created_at_dt < since:
                    continue
                if until is not None and created_at_dt > until:
                    continue
            out.append({"snapshot_id": sid, "created_at": snap["created_at"]})
        return out

    def restore(self, sid: str, caller_tenant_id: str) -> None:
        snap = self.snapshots.get(sid)
        if snap is None:
            from clinic_mock.errors import not_found

            raise not_found(f"snapshot {sid}")
        if snap["tenant_id"] != caller_tenant_id:
            # Cross-tenant access — existence hidden, same convention as
            # appointments / patients / slots reads.
            from clinic_mock.errors import not_found

            raise not_found(f"snapshot {sid}")
        data = snap["data"]
        self.patients = {k: Patient(**v) for k, v in data["patients"].items()}
        self.slots = {k: Slot(**v) for k, v in data["slots"].items()}
        self.appointments = {
            k: Appointment(**v) for k, v in data["appointments"].items()
        }
        self.writelog = WriteLog()
        self.writelog.entries = list(data.get("writelog", []))
        self.system_clock_offset_sec = data["system_clock_offset_sec"]

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
    tenants: list[str] = []
    for entry in raw.split(","):
        e = entry.strip()
        if not e:
            continue
        if ":" in e:
            tenant, key = (part.strip() for part in e.split(":", 1))
        else:
            key = e
            tenant = derive_tenant_id(key)
        if key.startswith("sk_") and tenant:
            tenants.append(tenant)
    if not tenants:
        tenants = [derive_tenant_id("sk_unset")]
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
