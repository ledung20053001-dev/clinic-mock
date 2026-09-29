"""In-memory store with snapshot/restore for test isolation.

Single global `db` instance; snapshot copies are deep via model_dump.

Canonical contract fixtures (Listing 3/4 ids: apt_00417, pt_3391, slot_91d2,
slot_77aa, cl_vinmec) are seeded once under the sentinel tenant `t_canonical`
and visible to every caller via `_visible_tenants()`. Per-tenant fixtures
keep the existing suffix trick so isolation tests still see distinct rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from clinic_mock.auth import derive_tenant_id
from clinic_mock.schemas import (
    Appointment,
    Patient,
    PatientRef,
    PatientVerify,
    Slot,
)

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


# ----- seed fixtures -----

CLINICS = [
    {"id": "c_001", "name": "Phòng khám Đa khoa Trung tâm"},
    {"id": "c_002", "name": "Phòng khám Đa khoa Cầu Giấy"},
    {"id": "cl_vinmec", "name": "Vinmec Times City"},
]

PROVIDERS = [
    {"id": "pr_456", "name": "Bác sĩ Nguyễn Văn An", "clinic_id": "c_001"},
    {"id": "pr_789", "name": "Bác sĩ Trần Thị Bình", "clinic_id": "c_002"},
    {"id": "pr_vinmec_1", "name": "Bác sĩ Phạm Thị Cúc", "clinic_id": "cl_vinmec"},
]

# Per-tenant fixtures — each row is replicated once per registered key, with
# the row id suffixed by a short tenant hash so the copies stay unique in the
# store while the row content is identical across callers.
PATIENT_FIXTURES = [
    {
        "id": "p_12345",
        "display_name": "Mai N.",
        "phone": "0912345678",
        "dob": "1985-04-12",
        "verify": {"full_name": "Nguyễn Thị Mai", "dob": "1985-04-12"},
    },
    {
        "id": "p_67890",
        "display_name": "Nam T.",
        "phone": "0987654321",
        "dob": "1972-11-03",
        "verify": {"full_name": "Trần Văn Nam", "dob": "1972-11-03"},
    },
]

SLOT_FIXTURES = [
    {
        "slot_id": "s_987",
        "clinic_id": "c_001",
        "start_time": "2026-09-15T09:00:00Z",
        "end_time": "2026-09-15T09:30:00Z",
        "provider_id": "pr_456",
    },
    {
        "slot_id": "s_988",
        "clinic_id": "c_001",
        "start_time": "2026-09-15T09:30:00Z",
        "end_time": "2026-09-15T10:00:00Z",
        "provider_id": "pr_456",
    },
    {
        "slot_id": "s_1024",
        "clinic_id": "c_002",
        "start_time": "2026-09-15T11:00:00Z",
        "end_time": "2026-09-15T11:30:00Z",
        "provider_id": "pr_789",
    },
]

# Canonical contract fixtures — Listing 3/4. Seeded once under t_canonical
# and visible to all tenants. apt_00417 consumes slot_77aa (NOT in db.slots).
CANONICAL_PATIENT_FIXTURES = [
    {
        "id": "pt_3391",
        "display_name": "N. V. A.",
        "phone": "0912345600",
        "dob": "1978-03-14",
        "verify": {"full_name": "Nguyễn Văn A", "dob": "1978-03-14"},
    },
]

CANONICAL_SLOT_FIXTURES = [
    # Listing 4 — the open slot the bot will pick during reschedule.
    {
        "slot_id": "slot_91d2",
        "clinic_id": "cl_vinmec",
        "start_time": "2026-10-14T15:00:00+07:00",
        "end_time": "2026-10-14T15:30:00+07:00",
        "provider_id": "pr_vinmec_1",
    },
]

CANONICAL_APPOINTMENT_FIXTURES = [
    # Listing 3 — apt_00417 occupies slot_77aa (which is NOT seeded in
    # db.slots). Listing 4's reschedule flow releases slot_77aa back.
    {
        "appointment_id": "apt_00417",
        "slot_id": "slot_77aa",
        "provider_id": "pr_vinmec_1",
        "status": "SCHEDULED",
        "clinic_id": "cl_vinmec",
        "starts_at": "2026-10-14T15:30:00+07:00",
        "ends_at": "2026-10-14T16:00:00+07:00",
        "department": "Nội tổng quát",
        "patient_id": "pt_3391",
        "attempt_count": 0,
        "version": 3,
    },
]


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


def _seed_canonical_patients() -> None:
    for f in CANONICAL_PATIENT_FIXTURES:
        db.patients[f["id"]] = Patient(
            patient_id=f["id"],
            tenant_id=CANONICAL_TENANT,
            display_name=f["display_name"],
            phone=f["phone"],
            dob=f["dob"],
            verify=PatientVerify(**f["verify"]),
        )


def _seed_canonical_slots() -> None:
    for f in CANONICAL_SLOT_FIXTURES:
        db.slots[f["slot_id"]] = Slot(
            slot_id=f["slot_id"],
            tenant_id=CANONICAL_TENANT,
            clinic_id=f["clinic_id"],
            start_time=f["start_time"],
            end_time=f["end_time"],
            provider_id=f["provider_id"],
        )


def _seed_canonical_appointments() -> None:
    for f in CANONICAL_APPOINTMENT_FIXTURES:
        patient = db.patients[f["patient_id"]]
        appt = Appointment(
            appointment_id=f["appointment_id"],
            tenant_id=CANONICAL_TENANT,
            slot_id=f["slot_id"],
            provider_id=f["provider_id"],
            status=f["status"],
            clinic_id=f["clinic_id"],
            starts_at=f["starts_at"],
            ends_at=f["ends_at"],
            department=f["department"],
            patient=PatientRef(
                patient_id=patient.patient_id,
                display_name=patient.display_name,
                verify=patient.verify,
            ),
            attempt_count=f["attempt_count"],
            version=f["version"],
        )
        db.appointments[f["appointment_id"]] = appt


def seed_default() -> None:
    """Populate db with the canonical mock fixtures (idempotent: reset first).

    Canonical contract fixtures seed once under `t_canonical` and are visible
    to every tenant. Per-tenant fixtures replicate per registered key with a
    short tenant hash suffix.
    """
    db.reset()
    db.system_clock_offset_sec = 0
    tenants = _seed_tenants()

    _seed_canonical_patients()
    _seed_canonical_slots()
    _seed_canonical_appointments()

    for tenant in tenants:
        suffix = _scope_suffix(tenant)
        for f in PATIENT_FIXTURES:
            pid = f"{f['id']}_{suffix}"
            db.patients[pid] = Patient(
                patient_id=pid,
                tenant_id=tenant,
                display_name=f["display_name"],
                phone=f["phone"],
                dob=f["dob"],
                verify=PatientVerify(**f["verify"]),
            )
        for f in SLOT_FIXTURES:
            sid = f"{f['slot_id']}_{suffix}"
            db.slots[sid] = Slot(
                slot_id=sid,
                tenant_id=tenant,
                clinic_id=f["clinic_id"],
                start_time=f["start_time"],
                end_time=f["end_time"],
                provider_id=f["provider_id"],
            )
