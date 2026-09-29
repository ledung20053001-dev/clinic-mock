"""Pydantic models — mirrors the AI Health Residency product contract (Rev 1.0).

Source of truth: §2.2 (end states), Appendix A (enumerations and error codes),
Listing 3 (GET /appointments/{id} shape), Listing 4 (POST /appointments/{id}/reschedule).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ----- shared field types -----

Phone = Annotated[str, StringConstraints(pattern=r"^(02|03|05|07|08|09)\d{8}$")]
# RFC 3339 — Listing 3 uses `+07:00` (offset); legacy `Z` (UTC) also accepted.
IsoDateTime = Annotated[
    str,
    StringConstraints(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
    ),
]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


# ----- patient / slot -----


class PatientVerify(BaseModel):
    """Identity block the bot checks against the caller's spoken name + dob."""

    full_name: str
    dob: IsoDate


class Patient(BaseModel):
    patient_id: str
    tenant_id: str = Field(exclude=True)
    display_name: str
    phone: Phone
    dob: IsoDate
    verify: PatientVerify | None = None


class PatientCreate(BaseModel):
    """Body for POST /_harness/patients — server generates the patient_id."""

    display_name: str
    phone: Phone
    dob: IsoDate
    verify: PatientVerify | None = None


class PatientUpdate(BaseModel):
    """Body for PATCH /_harness/patients/{id} — all fields optional."""

    display_name: str | None = None
    phone: Phone | None = None
    dob: IsoDate | None = None
    verify: PatientVerify | None = None


class Slot(BaseModel):
    slot_id: str
    tenant_id: str = Field(exclude=True)
    clinic_id: str
    start_time: IsoDateTime
    end_time: IsoDateTime
    provider_id: str


# ----- appointment -----

# Contract §2.2 — the seven end states. SCHEDULED is the default (counts as
# failed case unless the scenario expects it).
AppointmentStatus = Literal[
    "SCHEDULED",
    "BOOKED",
    "CONFIRMED",
    "CANCELLED",
    "RESCHEDULED",
    "TRANSFERRED",
    "UNREACHABLE",
]
CancelReason = Literal[
    "PATIENT_UNAVAILABLE",
    "NO_LONGER_NEEDED",
    "WENT_ELSEWHERE",
    "COST",
    "UNSPECIFIED",
]
TransferReason = Literal[
    "IDENTITY_FAILED",
    "PATIENT_NOT_FOUND",
    "OUT_OF_SCOPE",
    "CLINICAL_QUESTION",
    "NOT_UNDERSTOOD",
    "PATIENT_REQUEST",
    "SYSTEM_ERROR",
]
UnreachableReason = Literal[
    "SILENCE",
    "VOICEMAIL",
    "NO_ANSWER",
    "LINE_BUSY",
]
RescheduleRequestedBy = Literal["PATIENT", "STAFF"]


class PatientRef(BaseModel):
    """Embedded inside an Appointment per Listing 3."""

    patient_id: str
    display_name: str
    verify: PatientVerify


class Appointment(BaseModel):
    """Contract Listing 3 — canonical GET /appointments/{id} response."""

    appointment_id: str
    tenant_id: str = Field(exclude=True)
    # `slot_id` is server-side state used by reschedule to release the old slot
    # back into db.slots; never returned in responses (Listing 3 omits it).
    slot_id: str = Field(exclude=True)
    # `provider_id` is server-side state used to reconstruct the released Slot
    # on reschedule; never returned (Listing 3 omits it).
    provider_id: str = Field(exclude=True)
    status: AppointmentStatus
    clinic_id: str
    starts_at: IsoDateTime
    ends_at: IsoDateTime
    department: str
    patient: PatientRef
    cancel_reason: CancelReason | None = None
    transfer_reason: TransferReason | None = None
    unreachable_reason: UnreachableReason | None = None
    confirmed_at: IsoDateTime | None = None
    confirmed_via: str | None = None
    new_slot_id: str | None = None  # audit trail of the slot picked on reschedule
    attempt_count: int = 0
    version: int = 1


class AppointmentCreate(BaseModel):
    """§1.1.10 inbound booking — `starts_at`/`ends_at`/`department` derived
    from the resolved Slot when not supplied (post-§1.1.10 "exactly one record,
    on a slot GET /slots offered" → bot has a slot_id)."""

    patient_id: str
    slot_id: str


class CancelRequest(BaseModel):
    """§3.5 SF-05 — `confirmed` is the second explicit confirmation from the
    patient; the bot must obtain it before recording a cancellation."""

    cancel_reason: CancelReason
    confirmed: bool = False


class TransferRequest(BaseModel):
    """§2.2 TRANSFERRED + transfer_reason. The contract is a status flip —
    no sibling appointment is created at a target clinic."""

    transfer_reason: TransferReason


class UnreachableRequest(BaseModel):
    """§4.2.5 — body chỉ chứa unreachable_reason; attempt_count bị reject."""

    model_config = ConfigDict(extra="forbid")

    unreachable_reason: UnreachableReason


class RescheduleRequest(BaseModel):
    """Contract Listing 4 request body."""

    new_slot_id: str
    requested_by: RescheduleRequestedBy


class AppointmentRescheduleResponse(BaseModel):
    """Contract Listing 4 — minimal reschedule response (NOT the full appointment)."""

    status: AppointmentStatus
    new_slot_id: str
    released_slot_id: str
    version: int


class WriteHeaders(BaseModel):
    """§4.2.3 — `Idempotency-Key` MUST, `If-Match` SHOULD, on every write."""

    model_config = ConfigDict(populate_by_name=True)

    if_match: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)


# ----- pagination envelope -----


class Page(BaseModel):
    data: list
    next_cursor: str | None = None
    has_more: bool = False
