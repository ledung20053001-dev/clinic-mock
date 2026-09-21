"""All v1 + harness routes in one file.

Per the AI Health Residency product contract (Rev 1.0):
- §4.2 — clinic mock endpoints (this file).
- §4.2.3 — every write takes `Idempotency-Key` (MUST) and `If-Match` (SHOULD).
- §3.5 SF-05 — cancellation requires an explicit second confirmation.

The /v1/calls/* surface (the bot's contract per §4.1) is intentionally absent
from this mock. Calls are scored by the harness over HTTP directly against
the bot's /v1/calls endpoints; appointment state lives here.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status

from clinic_mock.auth import optional_principal
from clinic_mock.errors import (
    confirmation_required,
    conflict,
    not_found,
    unauthorized,
    unprocessable,
    validation_error,
    version_conflict,
)
from clinic_mock.lifecycle import assert_appointment_transition
from clinic_mock.schemas import (
    Appointment,
    AppointmentCreate,
    AppointmentRescheduleResponse,
    CancelRequest,
    Patient,
    PatientCreate,
    PatientVerify,
    RescheduleRequest,
    Slot,
    TransferRequest,
    UnreachableRequest,
    WriteHeaders,
)
from clinic_mock.store import (
    CANONICAL_TENANT,
    db,
    now_iso,
    provider_metadata,
    seed_default,
)


def _tenant(request: Request) -> str:
    """Resolve the caller's tenant id; raises 401 if auth didn't populate it."""
    principal = optional_principal(request)
    if principal is None:
        raise unauthorized()
    return principal.tenant_id


def _visible_tenants(caller_tenant: str) -> set[str]:
    """Tenant scopes visible to a caller.

    Contract canonical fixtures live under `t_canonical` and are visible to
    every caller so /v1/appointments/apt_00417 and friends resolve as the
    contract shows. Per-team fixtures (the existing p_12345/s_987 set) stay
    isolated to their own tenant.
    """
    return {caller_tenant, CANONICAL_TENANT}


def write_headers(
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=255)
    ] = None,
) -> WriteHeaders:
    """§4.2.3 dependency — typed accessor for the two required write headers."""
    return WriteHeaders(if_match=if_match, idempotency_key=idempotency_key)


def _check_version(appt: Appointment, if_match: int | None) -> None:
    """§4.2.3 — `If-Match` SHOULD match the last-read version."""
    if if_match is not None and if_match != appt.version:
        raise version_conflict(current=appt.version)


def _get_appt(appt_id: str, caller_tenant: str) -> Appointment:
    appt = db.appointments.get(appt_id)
    if not appt or appt.tenant_id not in _visible_tenants(caller_tenant):
        raise not_found(f"appointment {appt_id}")
    return appt


# ----- cursor pagination -----


def encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    pad = "=" * (-len(cursor) % 4)
    return json.loads(base64.urlsafe_b64decode(cursor + pad).decode())


def paginate(
    items: list, cursor: str | None, limit: int
) -> tuple[list, str | None, bool]:
    limit = max(1, min(limit, 100))
    start = 0
    if cursor:
        try:
            start = int(decode_cursor(cursor).get("offset", 0))
        except Exception:  # noqa: BLE001
            start = 0
    page = items[start : start + limit]
    next_offset = start + limit
    has_more = next_offset < len(items)
    next_cursor = encode_cursor({"offset": next_offset}) if has_more else None
    return page, next_cursor, has_more


# ----- routers -----

v1 = APIRouter(prefix="/v1")
harness = APIRouter(prefix="/_harness")
health = APIRouter(tags=["Health"])


@health.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


# ===== Discovery =====


@v1.get("/patients", tags=["Discovery"])
def find_patients(
    request: Request,
    phone: Annotated[str, Query(pattern=r"^(02|03|05|07|08|09)\d{8}$")],
    cursor: str | None = None,
    limit: int = 25,
):
    tenants = _visible_tenants(_tenant(request))
    matched = [
        p.model_dump()
        for p in db.patients.values()
        if p.tenant_id in tenants and p.phone == phone
    ]
    page, next_cursor, has_more = paginate(matched, cursor, limit)
    return {"data": page, "next_cursor": next_cursor, "has_more": has_more}


@v1.post("/patients", status_code=status.HTTP_201_CREATED, tags=["Patients"])
def create_patient(
    request: Request,
    body: PatientCreate,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    """Create a patient in the authenticated tenant.

    Phone is discovery metadata only; creating a record does not verify a
    caller's identity. Replayed requests return the original patient record.
    """

    tenant = _tenant(request)
    payload = body.model_dump()
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            "POST /v1/patients",
            payload,
        )
        if replay is not None:
            return Response(
                content=json.dumps(replay["body"]),
                status_code=replay["status"],
                headers={
                    "Idempotent-Replayed": "true",
                    "Content-Type": "application/json",
                },
            )

    first_name = body.first_name.strip()
    last_name = body.last_name.strip()
    address = body.address.strip()
    if not first_name or not last_name or not address:
        raise validation_error("first_name, last_name and address must not be blank.")
    if any(
        patient.tenant_id == tenant and patient.phone == body.phone
        for patient in db.patients.values()
    ):
        raise conflict(
            "PATIENT_PHONE_EXISTS",
            "A patient with this phone already exists in the tenant.",
        )

    patient = Patient(
        patient_id=db.new_id("p"),
        tenant_id=tenant,
        display_name=f"{first_name} {last_name[0]}.",
        phone=body.phone,
        dob=body.dob,
        address=address,
        verify=PatientVerify(
            full_name=f"{last_name} {first_name}",
            dob=body.dob,
        ),
    )
    db.patients[patient.patient_id] = patient
    response_body = patient.model_dump()
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            "POST /v1/patients",
            payload,
            status.HTTP_201_CREATED,
            response_body,
        )
    return response_body


@v1.get("/slots", tags=["Discovery"])
def list_slots(
    request: Request,
    clinic_id: str,
    from_: Annotated[
        str,
        Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"),
    ],
    to: Annotated[
        str, Query(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
    ],
    cursor: str | None = None,
    limit: int = 25,
):
    tenants = _visible_tenants(_tenant(request))
    if to <= from_:
        raise validation_error("'to' must be greater than 'from'.")
    f = datetime.fromisoformat(from_)
    t = datetime.fromisoformat(to)
    if (t - f) > timedelta(days=14):
        raise validation_error("'from'..'to' window must be <= 14 days.")

    # The cheap checks first: most slots belong to another key or clinic, and
    # schedules generate thousands. Earliest first, so offset cursors page stably.
    matched: list[tuple[datetime, str, Slot]] = []
    for slot in db.slots.values():
        if slot.tenant_id not in tenants or slot.clinic_id != clinic_id:
            continue
        starts = datetime.fromisoformat(slot.start_time)
        if f <= starts < t:
            matched.append((starts, slot.slot_id, slot))
    matched.sort(key=lambda row: (row[0], row[1]))
    page, next_cursor, has_more = paginate(matched, cursor, limit)
    return {
        "data": [slot.model_dump() for _, _, slot in page],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# ===== Appointments =====


@v1.post("/appointments", status_code=status.HTTP_201_CREATED, tags=["Appointments"])
def create_appointment(
    request: Request,
    body: AppointmentCreate,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            "POST /v1/appointments",
            body.model_dump(),
        )
        if replay is not None:
            return Response(
                content=json.dumps(replay["body"]),
                status_code=replay["status"],
                headers={
                    "Idempotent-Replayed": "true",
                    "Content-Type": "application/json",
                },
            )

    tenants = _visible_tenants(tenant)
    patient = db.patients.get(body.patient_id)
    if not patient or patient.tenant_id not in tenants:
        raise not_found(f"patient {body.patient_id}")
    slot = db.slots.get(body.slot_id)
    if not slot or slot.tenant_id not in tenants:
        raise not_found(f"slot {body.slot_id}")

    appt = Appointment(
        appointment_id=db.new_id("apt"),
        tenant_id=tenant,
        slot_id=slot.slot_id,
        provider_id=slot.provider_id,
        provider_name=slot.provider_name,
        status="BOOKED",
        clinic_id=slot.clinic_id,
        starts_at=slot.start_time,
        ends_at=slot.end_time,
        department=slot.department,
        patient=_patient_ref(patient),
        attempt_count=0,
        version=1,
    )
    db.appointments[appt.appointment_id] = appt
    db.slots.pop(slot.slot_id, None)  # consumed

    body_out = appt.model_dump()
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            "POST /v1/appointments",
            body.model_dump(),
            201,
            body_out,
        )
    return body_out


def _patient_ref(patient: Patient):
    """Build an Appointment.patient block (Listing 3 shape) from a Patient row."""
    from clinic_mock.schemas import PatientRef

    return PatientRef(
        patient_id=patient.patient_id,
        display_name=patient.display_name,
        verify=patient.verify,
    )


@v1.get("/appointments/{appt_id}", tags=["Appointments"])
def get_appointment(request: Request, appt_id: str):
    return _get_appt(appt_id, _tenant(request)).model_dump()


@v1.get("/appointments", tags=["Appointments"])
def list_appointments(
    request: Request,
    date: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
    clinic_id: str,
    cursor: str | None = None,
    limit: int = 25,
):
    tenants = _visible_tenants(_tenant(request))
    matched = [
        a.model_dump()
        for a in db.appointments.values()
        if a.tenant_id in tenants
        and a.clinic_id == clinic_id
        and a.starts_at.startswith(date)
    ]
    matched.sort(key=lambda a: a["starts_at"])
    page, next_cursor, has_more = paginate(matched, cursor, limit)
    return {"data": page, "next_cursor": next_cursor, "has_more": has_more}


@v1.post("/appointments/{appt_id}/confirm", tags=["Appointments"])
def confirm_appointment(
    request: Request,
    appt_id: str,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/confirm",
            {"if_match": headers.if_match},
        )
        if replay is not None:
            return replay["body"]
    appt = _get_appt(appt_id, tenant)
    _check_version(appt, headers.if_match)
    assert_appointment_transition(appt.status, "confirm")
    updated = appt.model_copy(
        update={
            "status": "CONFIRMED",
            "confirmed_at": now_iso(),
            "confirmed_via": "callbot",
            "version": appt.version + 1,
        }
    )
    db.appointments[appt_id] = updated
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/confirm",
            {"if_match": headers.if_match},
            200,
            updated.model_dump(),
        )
    return updated.model_dump()


@v1.post("/appointments/{appt_id}/cancel", tags=["Appointments"])
def cancel_appointment(
    request: Request,
    appt_id: str,
    body: CancelRequest,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/cancel",
            {"if_match": headers.if_match, **body.model_dump()},
        )
        if replay is not None:
            return replay["body"]
    appt = _get_appt(appt_id, tenant)
    _check_version(appt, headers.if_match)
    if not body.confirmed:
        raise confirmation_required(
            "Cancellation requires an explicit second confirmation."
        )
    assert_appointment_transition(appt.status, "cancel")
    updated = appt.model_copy(
        update={
            "status": "CANCELLED",
            "cancel_reason": body.cancel_reason,
            "version": appt.version + 1,
        }
    )
    db.appointments[appt_id] = updated
    # Cancellation releases the consumed slot so another patient can book it.
    db.slots[appt.slot_id] = Slot(
        slot_id=appt.slot_id,
        tenant_id=appt.tenant_id,
        clinic_id=appt.clinic_id,
        start_time=appt.starts_at,
        end_time=appt.ends_at,
        provider_id=appt.provider_id,
        provider_name=appt.provider_name or provider_metadata(appt.provider_id)["name"],
        department=appt.department,
    )
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/cancel",
            {"if_match": headers.if_match, **body.model_dump()},
            200,
            updated.model_dump(),
        )
    return updated.model_dump()


@v1.post("/appointments/{appt_id}/transfer", tags=["Appointments"])
def transfer_appointment(
    request: Request,
    appt_id: str,
    body: TransferRequest,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/transfer",
            {"if_match": headers.if_match, **body.model_dump()},
        )
        if replay is not None:
            return replay["body"]
    appt = _get_appt(appt_id, tenant)
    _check_version(appt, headers.if_match)
    assert_appointment_transition(appt.status, "transfer")
    updated = appt.model_copy(
        update={
            "status": "TRANSFERRED",
            "transfer_reason": body.transfer_reason,
            "version": appt.version + 1,
        }
    )
    db.appointments[appt_id] = updated
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/transfer",
            {"if_match": headers.if_match, **body.model_dump()},
            200,
            updated.model_dump(),
        )
    return updated.model_dump()


@v1.post("/appointments/{appt_id}/reschedule", tags=["Appointments"])
def reschedule_appointment(
    request: Request,
    appt_id: str,
    body: RescheduleRequest,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/reschedule",
            {"if_match": headers.if_match, **body.model_dump()},
        )
        if replay is not None:
            return replay["body"]
    appt = _get_appt(appt_id, tenant)
    _check_version(appt, headers.if_match)
    assert_appointment_transition(appt.status, "reschedule")
    tenants = _visible_tenants(tenant)
    new_slot = db.slots.get(body.new_slot_id)
    if not new_slot or new_slot.tenant_id not in tenants:
        raise conflict("SLOT_TAKEN", f"{body.new_slot_id} is no longer open")

    # Release the OLD slot back into db.slots keyed by its original slot_id.
    released_slot_id = appt.slot_id
    if released_slot_id:
        db.slots[released_slot_id] = Slot(
            slot_id=released_slot_id,
            tenant_id=appt.tenant_id,
            clinic_id=appt.clinic_id,
            start_time=appt.starts_at,
            end_time=appt.ends_at,
            provider_id=appt.provider_id,
            provider_name=appt.provider_name or provider_metadata(appt.provider_id)["name"],
            department=appt.department,
        )
    db.slots.pop(new_slot.slot_id, None)

    new_version = appt.version + 1
    updated = appt.model_copy(
        update={
            "slot_id": new_slot.slot_id,
            "provider_id": new_slot.provider_id,
            "provider_name": new_slot.provider_name,
            "status": "RESCHEDULED",
            "clinic_id": new_slot.clinic_id,
            "starts_at": new_slot.start_time,
            "ends_at": new_slot.end_time,
            "department": new_slot.department,
            "new_slot_id": new_slot.slot_id,
            "version": new_version,
        }
    )
    db.appointments[appt_id] = updated
    response = AppointmentRescheduleResponse(
        status="RESCHEDULED",
        new_slot_id=new_slot.slot_id,
        released_slot_id=released_slot_id,
        version=new_version,
    ).model_dump()
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/reschedule",
            {"if_match": headers.if_match, **body.model_dump()},
            200,
            response,
        )
    return response


@v1.post("/appointments/{appt_id}/unreachable", tags=["Appointments"])
def mark_unreachable(
    request: Request,
    appt_id: str,
    body: UnreachableRequest,
    headers: Annotated[WriteHeaders, Depends(write_headers)],
):
    """§4.2.5 — UNREACHABLE end state (no answer / voicemail / line busy).
    Idempotent: each call increments attempt_count and (re-)confirms UNREACHABLE,
    so the harness can record multiple no-answer attempts on the same appointment.
    Idempotency-Key is evaluated before If-Match per §4.2.5.
    """
    tenant = _tenant(request)
    if headers.idempotency_key:
        replay = _idempotent_check(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/unreachable",
            body.model_dump(),
        )
        if replay is not None:
            return replay["body"]
    appt = _get_appt(appt_id, tenant)
    _check_version(appt, headers.if_match)
    if appt.status not in {"SCHEDULED", "BOOKED", "UNREACHABLE"}:
        assert_appointment_transition(appt.status, "unreachable")
    updated = appt.model_copy(
        update={
            "status": "UNREACHABLE",
            "unreachable_reason": body.unreachable_reason,
            "attempt_count": appt.attempt_count + 1,
            "version": appt.version + 1,
        }
    )
    db.appointments[appt_id] = updated
    if headers.idempotency_key:
        _idempotent_store(
            headers.idempotency_key,
            f"POST /v1/appointments/{appt_id}/unreachable",
            body.model_dump(),
            200,
            updated.model_dump(),
        )
    return updated.model_dump()


# ===== Harness (tenant-scoped, reads also see canonical fixtures) =====


@harness.get("/state", tags=["Admin"])
def harness_state(request: Request):
    tenants = _visible_tenants(_tenant(request))
    return {
        "patients": [
            p.model_dump() for p in db.patients.values() if p.tenant_id in tenants
        ],
        "slots": [
            s.model_dump() for s in db.slots.values() if s.tenant_id in tenants
        ],
        "appointments": [
            a.model_dump() for a in db.appointments.values() if a.tenant_id in tenants
        ],
    }


@harness.get("/patients", tags=["Admin"])
def harness_patients(request: Request):
    tenants = _visible_tenants(_tenant(request))
    return [p.model_dump() for p in db.patients.values() if p.tenant_id in tenants]


@harness.get("/slots", tags=["Admin"])
def harness_slots(request: Request):
    tenants = _visible_tenants(_tenant(request))
    return [s.model_dump() for s in db.slots.values() if s.tenant_id in tenants]


@harness.get("/appointments", tags=["Admin"])
def harness_appointments(request: Request):
    tenants = _visible_tenants(_tenant(request))
    return [
        a.model_dump() for a in db.appointments.values() if a.tenant_id in tenants
    ]


@harness.get("/snapshot", tags=["Admin"])
def harness_snapshot(request: Request):
    sid = db.snapshot()
    return {"snapshot_id": sid}


@harness.post("/snapshot/{sid}/restore", tags=["Admin"])
def harness_snapshot_restore(request: Request, sid: str):
    db.restore(sid)
    return {"restored": sid}


@harness.post("/seed", tags=["Admin"])
def harness_seed(request: Request):
    seed_default()
    return {"seeded": True}


@harness.post("/reset", tags=["Admin"])
def harness_reset(request: Request):
    db.reset()
    seed_default()  # ponytail: re-seed so /state isn't empty after a reset
    return {"reset": True}


@harness.post("/time-travel", tags=["Admin"])
def harness_time_travel(request: Request, body: dict = Body(default={})):  # noqa: B008
    seconds = int(body.get("seconds", 0))
    db.system_clock_offset_sec += seconds
    return {"offset_seconds": db.system_clock_offset_sec}


# ----- Idempotency cache (in-memory, 24h) -----

_idempotency: dict[str, dict[str, Any]] = {}


def _idempotent_check(
    key: str, endpoint: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    slot = _idempotency.get(f"{endpoint}:{key}")
    if not slot:
        return None
    if slot["expires_at"] < datetime.now(UTC):
        _idempotency.pop(f"{endpoint}:{key}", None)
        return None
    if slot["payload_hash"] != hash(json.dumps(payload, sort_keys=True)):
        raise unprocessable(
            "IDEMPOTENCY_CONFLICT", "Idempotency-Key reused with a different payload."
        )
    return slot["response"]


def _idempotent_store(
    key: str,
    endpoint: str,
    payload: dict[str, Any],
    status_code: int,
    response_body: dict[str, Any],
) -> None:
    _idempotency[f"{endpoint}:{key}"] = {
        "payload_hash": hash(json.dumps(payload, sort_keys=True)),
        "response": {"status": status_code, "body": response_body},
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
    }
